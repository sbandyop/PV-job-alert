"""
Weekly PV Job Alert — Soumi Bandyopadhyay
Free tools only: Adzuna API + employer scrapers + keyword scoring + Gmail SMTP

Pipeline:
  1. Adzuna search -> hard filter chain (CH, language, function) -> cooldown -> score
  2. Swiss employer direct scrape (filter chain applied internally) -> cooldown
  3. Combine -> single email

Blocklists:
  - REJECTED_COMPANIES (this file): hardcoded permanent blocks
  - rejection_cooldowns.json: time-based cooldowns from explicit/auto-ATS rejections

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
from rejection_cooldowns import load_cooldowns, is_blocked, format_expiring_soon

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

# Hardcoded PERMANENT blocks — genuine never-apply decisions (not rejections).
# Rejections go in rejection_cooldowns.json with expiry dates.
#
# Audit trail — removed from this list 2026-05-27:
#   ewz          → removed entirely (Talent Pool invite, no rejection)
#   enshift      → rejection_cooldowns.json, role-specific 6mo (language only)
#   gruner       → rejection_cooldowns.json, role-specific 12mo
#   primeo       → rejection_cooldowns.json, role-specific 12mo
#   solarmarkt   → removed (no evidence of rejection found in Gmail)
#   aventron     → removed (no evidence of rejection found in Gmail)
#   bakerhicks   → removed (no evidence of rejection found in Gmail)
#   agap2        → removed (no evidence of rejection found in Gmail)
REJECTED_COMPANIES: list[str] = [
    # Add genuine never-apply companies here (e.g. known bad employers, competitors).
    # Currently empty — all known rejections are time-based in rejection_cooldowns.json.
]

# ─── SCORING ─────────────────────────────────────────────────────────────────
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


def is_rejected_permanent(company):
    return any(r in company.lower() for r in REJECTED_COMPANIES)


def process_adzuna(raw_jobs, cooldowns):
    matches = []
    for job in raw_jobs:
        title   = job.get("title", "")
        company = job.get("company", {}).get("display_name", "")
        loc     = job.get("location", {}).get("display_name", "")
        desc    = job.get("description", "")
        link    = job.get("redirect_url", "")

        if is_rejected_permanent(company):
            print(f"  SKIP (permanent block): {company}")
            continue

        blocked, entry = is_blocked(company, title, cooldowns)
        if blocked:
            print(f"  SKIP (cooldown until {entry['blocked_until']}): {title} @ {company}")
            continue

        jd_body = fetch_jd_body(link) if link else ""

        keep, reason = apply_filter_chain(
            title=title, location=loc, jd_body=jd_body,
            workmode="", short_description=desc,
        )
        if not keep:
            print(f"  FILTER ({reason}): {title}")
            continue

        score, verdict, key_match, key_gap = score_job(title, company, desc)
        print(f"  {verdict} ({score}%): {title} @ {company}")

        if verdict == "Apply":
            matches.append({
                "source": "adzuna",
                "title": title, "company": company, "location": loc,
                "score": score, "key_match": key_match, "key_gap": key_gap,
                "link": link,
            })
    return matches


def filter_swiss_by_cooldown(swiss_jobs, cooldowns):
    """Apply cooldown filter to Swiss employer scraper results."""
    kept = []
    for j in swiss_jobs:
        if is_rejected_permanent(j.get("company", "")):
            print(f"  SWISS SKIP (permanent): {j['company']}")
            continue
        blocked, entry = is_blocked(j.get("company", ""), j.get("title", ""), cooldowns)
        if blocked:
            print(f"  SWISS SKIP (cooldown until {entry['blocked_until']}): {j['title']}")
            continue
        kept.append(j)
    return kept


# ─── EMAIL ───────────────────────────────────────────────────────────────────

def send_email(adzuna_matches, swiss_matches, expiring_cooldowns):
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
        body += "All results were filtered out by CH/language/function/cooldown checks.\n\n"

    if expiring_cooldowns:
        body += "\n### COOLDOWN EXPIRING SOON (<30 days)\n\n"
        body += "These companies will become eligible for re-application:\n\n"
        for e in expiring_cooldowns:
            scope_note = "company-wide" if e["block_scope"] == "company" else "role-specific"
            body += f"  • {e['company']:25s} expires {e['blocked_until']} "
            body += f"({e['days_remaining']} days, {scope_note}, {e['rejection_type']})\n"
        body += "\n"

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
        print("Search is PAUSED. Edit config.json to resume.")
        return

    cooldowns = load_cooldowns("rejection_cooldowns.json")
    print(f"Loaded {len(cooldowns)} active cooldown(s)\n")

    print("--- ADZUNA PIPELINE ---")
    raw_jobs = search_adzuna()
    adzuna_matches = process_adzuna(raw_jobs, cooldowns)

    print("\n--- SWISS EMPLOYER DIRECT SCRAPE ---")
    try:
        swiss_raw = fetch_swiss_employer_jobs(state_path="seen_swiss_jobs.json")
        swiss_matches = filter_swiss_by_cooldown(swiss_raw, cooldowns)
    except Exception as e:
        print(f"[WARN] Swiss scrape failed: {e}")
        swiss_matches = []

    expiring = format_expiring_soon(cooldowns, days=30)

    print(f"\n--- SUMMARY ---")
    print(f"Adzuna matches: {len(adzuna_matches)}")
    print(f"Swiss direct matches: {len(swiss_matches)}")
    print(f"Cooldowns expiring soon: {len(expiring)}")

    if adzuna_matches or swiss_matches or expiring:
        send_email(adzuna_matches, swiss_matches, expiring)
    else:
        print("No matches and no expiring cooldowns — no email sent.")


if __name__ == "__main__":
    main()
