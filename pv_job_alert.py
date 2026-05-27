"""
Weekly PV Job Alert — Soumi Bandyopadhyay
Free tools only: Adzuna API + employer scrapers + keyword scoring + Gmail SMTP

Pipeline:
  1. Adzuna search (existing) -> hard filter chain (CH, language, function) -> score
  2. Swiss employer direct scrape (new) -> hard filter chain
  3. Combine -> single email

Pause by editing config.json on GitHub.
"""

import os
import json
import requests
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

from job_filters import apply_filter_chain, fetch_jd_body
from swiss_employers import fetch_swiss_employer_jobs

# ─── SECRETS ────────────────────────────────────────────────────────────────
ADZUNA_APP_ID   = os.environ["ADZUNA_APP_ID"]
ADZUNA_APP_KEY  = os.environ["ADZUNA_APP_KEY"]
EMAIL_SENDER    = os.environ["EMAIL_SENDER"]
EMAIL_PASSWORD  = os.environ["EMAIL_PASSWORD"]
EMAIL_RECIPIENT = os.environ["EMAIL_RECIPIENT"]

# ─── SEARCH QUERIES ──────────────────────────────────────────────────────────
QUERIES = [
    "Projektleiter Photovoltaik",
    "Projektleiter Solar",
    "Projektentwickler PV",
    "Technical Project Manager Solar",
    "EPC Solar Project Manager",
    "Owner Engineer Renewable Energy",
    "Projektleiter Solarpark",
    "PV Projektmanager",
]

REJECTED_COMPANIES = [
    "enshift", "gruner", "aventron", "primeo energie",
    "solarmarkt", "ewz", "bakerhicks", "agap2"
]

# ─── SCORING (existing logic, unchanged for jobs that pass the new filter) ──
MATCH_SIGNALS = [
    (["photovoltaik", "photovoltaic", "pv-anlage", "solarpark",
      "solar pv", "solaranlage", "solar energy"], 25),
    (["projektleiter", "projektmanager", "project manager",
      "owner's engineer", "epc", "bauherr", "inbetriebnahme",
      "ausschreibung", "tendering", "commissioning"], 25),
    (["hybrid", "homeoffice", "remote", "basel", "zürich",
      "zurich", "bern", "schweiz", "switzerland"], 10),
    (["5 jahre", "5 years", "senior", "erfahrung", "experience"], 10),
    (["dachanlage", "rooftop", "gebäude", "commercial pv",
      "gewerblich", "industriedach"], 10),
]

# Note: tightened — removed "personalverantwortung", "teamleiter",
# "führung von mitarbeitenden", "disziplinarische führung" because these
# describe normal senior PM responsibilities you're qualified for.
# Language and physical-work blockers retained.
HARD_BLOCKERS = [
    "elektroinstallateur efz",
    "montage-elektriker",
    "französisch zwingend",
    "french mandatory",
    "french fluent required",
    "auf dächern",
    "auf dem dach",
    "psa",
    "dachdecker",
    "monteur",
    "10-20 stunden",
    "studentenjob",
]

DOMAIN_MISMATCH = [
    "wasserkraft", "hydro", "wärme", "steam turbine",
    "quantum", "pharma", "rolling stock", "automation",
    "buchhaltung", "accountant", "hr ", "informatik",
    "netzelektriker", "dachmonteur", "solarteur",
]


def score_job(title, company, description):
    text = (title + " " + description).lower()

    if any(kw in text for kw in DOMAIN_MISMATCH):
        return 0, "Skip", "Domain mismatch", ""

    blocker_hit = next((kw for kw in HARD_BLOCKERS if kw in text), None)

    score = 0
    for keywords, points in MATCH_SIGNALS:
        if any(kw in text for kw in keywords):
            score += points

    if blocker_hit:
        score = min(score, 25)
        verdict = "Skip"
        key_gap = f"Hard blocker: {blocker_hit}"
    elif score >= 50:
        verdict = "Apply"
        key_gap = ""
    else:
        verdict = "Skip"
        key_gap = "Insufficient PV/PM signal"

    matched = []
    if any(kw in text for kw in ["photovoltaik", "photovoltaic", "solar pv", "solarpark"]):
        matched.append("PV domain")
    if any(kw in text for kw in ["epc", "ausschreibung", "tendering"]):
        matched.append("EPC/tendering")
    if any(kw in text for kw in ["projektleiter", "project manager", "owner"]):
        matched.append("PM/OE function")
    if any(kw in text for kw in ["hybrid", "homeoffice"]):
        matched.append("Hybrid work")

    key_match = ", ".join(matched) if matched else "Partial signal"

    return score, verdict, key_match, key_gap


# ─── ADZUNA SEARCH ───────────────────────────────────────────────────────────

def search_adzuna():
    jobs = []
    seen = set()
    for query in QUERIES:
        try:
            resp = requests.get(
                "https://api.adzuna.com/v1/api/jobs/ch/search/1",
                params={
                    "app_id": ADZUNA_APP_ID,
                    "app_key": ADZUNA_APP_KEY,
                    "what": query,
                    "results_per_page": 10,
                    "max_days_old": 7,
                    "content-type": "application/json",
                },
                timeout=15
            )
            if resp.status_code == 200:
                for job in resp.json().get("results", []):
                    jid = job.get("id", "")
                    if jid and jid not in seen:
                        seen.add(jid)
                        jobs.append(job)
        except Exception as e:
            print(f"  Adzuna error for '{query}': {e}")
    print(f"Retrieved {len(jobs)} jobs from Adzuna")
    return jobs


def is_rejected(company):
    return any(r in company.lower() for r in REJECTED_COMPANIES)


def process_adzuna(raw_jobs):
    """
    For each Adzuna job:
      1. Reject if blocked company
      2. Apply hard filter chain (CH, function, language, etc.)
         — fetches JD body from redirect_url for accurate language detection
      3. Score with existing scoring engine
      4. Keep only Apply-verdict jobs scoring >= 50%
    """
    matches = []
    for job in raw_jobs:
        title   = job.get("title", "")
        company = job.get("company", {}).get("display_name", "")
        loc     = job.get("location", {}).get("display_name", "")
        desc    = job.get("description", "")
        link    = job.get("redirect_url", "")

        if is_rejected(company):
            print(f"  SKIP (blocked company): {company}")
            continue

        # Fetch full JD body from the redirect URL for language detection.
        # If fetch fails, falls back to Adzuna's short description.
        jd_body = fetch_jd_body(link) if link else ""

        keep, reason = apply_filter_chain(
            title=title,
            location=loc,
            jd_body=jd_body,
            workmode="",  # Adzuna doesn't reliably expose workmode
            short_description=desc,
        )
        if not keep:
            print(f"  FILTER ({reason}): {title}")
            continue

        score, verdict, key_match, key_gap = score_job(title, company, desc)
        print(f"  {verdict} ({score}%): {title} @ {company}")

        if verdict == "Apply":
            matches.append({
                "source":    "adzuna",
                "title":     title,
                "company":   company,
                "location":  loc,
                "score":     score,
                "key_match": key_match,
                "key_gap":   key_gap,
                "link":      link,
            })
    return matches


# ─── EMAIL ───────────────────────────────────────────────────────────────────

def send_email(adzuna_matches, swiss_matches):
    total = len(adzuna_matches) + len(swiss_matches)
    date_str = datetime.now().strftime("%d %B %Y")

    body = f"Weekly PV Job Alert — {date_str}\n"
    body += f"{total} role(s) matched your profile\n"
    body += "=" * 60 + "\n\n"

    if adzuna_matches:
        body += f"### ADZUNA AGGREGATOR ({len(adzuna_matches)})\n\n"
        for j in adzuna_matches:
            body += f"ROLE:      {j['title']}\n"
            body += f"COMPANY:   {j['company']}\n"
            body += f"LOCATION:  {j['location']}\n"
            body += f"FIT:       {j['score']}%\n"
            body += f"MATCH:     {j['key_match']}\n"
            if j['key_gap']:
                body += f"GAP:       {j['key_gap']}\n"
            body += f"LINK:      {j['link']}\n"
            body += "-" * 40 + "\n\n"

    if swiss_matches:
        body += f"\n### SWISS EMPLOYER DIRECT ({len(swiss_matches)})\n\n"
        for j in swiss_matches:
            body += f"ROLE:      {j['title']}\n"
            body += f"COMPANY:   {j['company']}\n"
            body += f"LOCATION:  {j['location']}\n"
            body += f"LINK:      {j['url']}\n"
            body += "-" * 40 + "\n\n"

    if not adzuna_matches and not swiss_matches:
        body += "No matches this week.\n"
        body += "All Adzuna results were filtered out by CH/language/function checks.\n"
        body += "All Swiss employer direct results were either already-seen or filtered.\n"

    msg = MIMEMultipart()
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = EMAIL_RECIPIENT
    msg["Subject"] = f"[PV Job Alert] {total} match(es) — {date_str}"
    msg.attach(MIMEText(body, "plain"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.send_message(msg)

    print(f"Email sent — {total} match(es)")


# ─── PAUSE ───────────────────────────────────────────────────────────────────

def is_paused():
    try:
        with open("config.json") as f:
            return json.load(f).get("paused", False)
    except FileNotFoundError:
        return False


# ─── MAIN ────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{'='*60}")
    print(f"PV Job Alert — {datetime.now().strftime('%d %B %Y %H:%M')}")
    print(f"{'='*60}\n")

    if is_paused():
        print("Search is PAUSED. Edit config.json and set paused to false to resume.")
        return

    # 1. Adzuna pipeline (with hard filter chain applied)
    print("\n--- ADZUNA PIPELINE ---")
    raw_jobs = search_adzuna()
    adzuna_matches = process_adzuna(raw_jobs)

    # 2. Swiss employer direct scrape (isolated — won't break Adzuna if it fails)
    print("\n--- SWISS EMPLOYER DIRECT SCRAPE ---")
    try:
        swiss_matches = fetch_swiss_employer_jobs(state_path="seen_swiss_jobs.json")
    except Exception as e:
        print(f"[WARN] Swiss scrape failed: {e}")
        swiss_matches = []

    print(f"\n--- SUMMARY ---")
    print(f"Adzuna matches: {len(adzuna_matches)}")
    print(f"Swiss direct matches: {len(swiss_matches)}")

    # 3. Email if there's anything to send
    if adzuna_matches or swiss_matches:
        send_email(adzuna_matches, swiss_matches)
    else:
        print("No matches — no email sent.")


if __name__ == "__main__":
    main()
