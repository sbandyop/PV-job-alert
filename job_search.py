"""
Weekly PV Job Alert — Soumi Bandyopadhyay
Free tools only: Adzuna API + keyword scoring + Gmail SMTP
Pause by editing config.json on GitHub
"""

import os
import json
import requests
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

# ─── SECRETS (set in GitHub → Settings → Secrets) ────────────────────────────
ADZUNA_APP_ID  = os.environ["ADZUNA_APP_ID"]
ADZUNA_APP_KEY = os.environ["ADZUNA_APP_KEY"]
EMAIL_SENDER   = os.environ["EMAIL_SENDER"]    # your Gmail
EMAIL_PASSWORD = os.environ["EMAIL_PASSWORD"]  # Gmail App Password
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

# ─── REJECTED COMPANIES ──────────────────────────────────────────────────────
REJECTED_COMPANIES = [
    "enshift", "gruner", "aventron", "primeo energie",
    "solarmarkt", "ewz", "bakerhicks", "agap2"
]

# ─── SCORING KEYWORDS ────────────────────────────────────────────────────────

# Each group: (keywords_to_check, points_if_any_match)
MATCH_SIGNALS = [
    # Domain — PV/Solar
    (["photovoltaik", "photovoltaic", "pv-anlage", "solarpark",
      "solar pv", "solaranlage", "solar energy"], 25),
    # Function — PM/OE
    (["projektleiter", "projektmanager", "project manager",
      "owner's engineer", "epc", "bauherr", "inbetriebnahme",
      "ausschreibung", "tendering", "commissioning"], 25),
    # Hybrid / Swiss location
    (["hybrid", "homeoffice", "remote", "basel", "zürich",
      "zurich", "bern", "schweiz", "switzerland"], 10),
    # Seniority match
    (["5 jahre", "5 years", "senior", "erfahrung", "experience"], 10),
    # Rooftop PV (also acceptable)
    (["dachanlage", "rooftop", "gebäude", "commercial pv",
      "gewerblich", "industriedach"], 10),
]

# Hard blocker keywords — any match caps score at 25 and forces Skip
HARD_BLOCKERS = [
    "elektroinstallateur efz",
    "montage-elektriker",
    "personalverantwortung",
    "teamleiter",    # only if combined with staff management
    "führung von mitarbeitenden",
    "disziplinarische führung",
    "französisch zwingend",
    "french mandatory",
    "french fluent required",
    "auf dächern",      # physical rooftop climbing
    "auf dem dach",
    "psa",              # fall protection = physical work
    "dachdecker",
    "monteur",
    "10-20 stunden",
    "studentenjob",
]

# Weak signals — if title has these alone with no PV context, skip
DOMAIN_MISMATCH = [
    "wasserkraft", "hydro", "wärme", "steam turbine",
    "quantum", "pharma", "rolling stock", "automation",
    "buchhaltung", "accountant", "hr ", "informatik",
    "netzelektriker", "dachmonteur", "solarteur",
]

# ─── SCORING ENGINE ──────────────────────────────────────────────────────────

def score_job(title, company, description):
    text = (title + " " + description).lower()

    # Domain mismatch — immediate skip
    if any(kw in text for kw in DOMAIN_MISMATCH):
        return 0, "Skip", "Domain mismatch", ""

    # Hard blockers — cap at 25
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

    # Key match summary
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

# ─── EMAIL ───────────────────────────────────────────────────────────────────

def send_email(matches):
    date_str = datetime.now().strftime("%d %B %Y")
    body = f"Weekly PV Job Alert — {date_str}\n"
    body += f"{len(matches)} role(s) matched your profile (≥50% fit):\n"
    body += "=" * 60 + "\n\n"

    for j in matches:
        body += f"ROLE:      {j['title']}\n"
        body += f"COMPANY:   {j['company']}\n"
        body += f"LOCATION:  {j['location']}\n"
        body += f"FIT:       {j['score']}%\n"
        body += f"MATCH:     {j['key_match']}\n"
        if j['key_gap']:
            body += f"GAP:       {j['key_gap']}\n"
        body += f"LINK:      {j['link']}\n"
        body += "-" * 40 + "\n\n"

    msg = MIMEMultipart()
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = EMAIL_RECIPIENT
    msg["Subject"] = f"[PV Job Alert] {len(matches)} Match(es) — {date_str}"
    msg.attach(MIMEText(body, "plain"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.send_message(msg)

    print(f"Email sent — {len(matches)} match(es)")

# ─── PAUSE CHECK ─────────────────────────────────────────────────────────────

def is_paused():
    try:
        with open("config.json") as f:
            return json.load(f).get("paused", False)
    except FileNotFoundError:
        return False

# ─── MAIN ────────────────────────────────────────────────────────────────────

def is_rejected(company):
    return any(r in company.lower() for r in REJECTED_COMPANIES)

def main():
    print(f"\n{'='*60}")
    print(f"PV Job Alert — {datetime.now().strftime('%d %B %Y %H:%M')}")
    print(f"{'='*60}\n")

    if is_paused():
        print("Search is PAUSED. Edit config.json and set paused to false to resume.")
        return

    raw_jobs = search_adzuna()
    matches = []

    for job in raw_jobs:
        title   = job.get("title", "")
        company = job.get("company", {}).get("display_name", "")
        loc     = job.get("location", {}).get("display_name", "")
        desc    = job.get("description", "")
        link    = job.get("redirect_url", "")

        if is_rejected(company):
            print(f"Skipping rejected company: {company}")
            continue

        score, verdict, key_match, key_gap = score_job(title, company, desc)
        print(f"{verdict} ({score}%): {title} @ {company}")

        if verdict == "Apply":
            matches.append({
                "title":     title,
                "company":   company,
                "location":  loc,
                "score":     score,
                "key_match": key_match,
                "key_gap":   key_gap,
                "link":      link,
            })

    print(f"\nMatches: {len(matches)}")
    if matches:
        send_email(matches)
    else:
        print("No matches — no email sent.")

if __name__ == "__main__":
    main()
