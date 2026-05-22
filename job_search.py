"""
Weekly PV Job Alert — Soumi Bandyopadhyay
Searches LinkedIn via Apify, scores with Claude API, emails matches ≥50%
"""

import os
import json
import time
import requests
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

# ─── CONFIG ──────────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
APIFY_API_KEY     = os.environ["APIFY_API_KEY"]
EMAIL_SENDER      = os.environ["EMAIL_SENDER"]       # your Gmail address
EMAIL_PASSWORD    = os.environ["EMAIL_PASSWORD"]     # Gmail App Password
EMAIL_RECIPIENT   = os.environ["EMAIL_RECIPIENT"]    # where to send alerts

CV_PROFILE = """
Role: Technical Project Manager / Owner's Engineer
Experience: 5 years post-MSc
PV scope: utility-scale and rooftop PV (26–102 MWp), BESS (up to 250 MW)
Core skills: EPC tendering & award, grid operator interface (technical clarifications),
  construction-phase oversight, commissioning acceptance (document review),
  contractual/commercial management, stakeholder management
Education: M.Sc. Sustainable Systems Engineering (Photovoltaics),
  Albert-Ludwigs-Universität Freiburg
Languages: English (fluent), German (B1, B2 in progress)
Location: Freiburg, Germany. Target: Basel or Zürich, Switzerland (hybrid preferred).
  Eligible for Grenzgängerbewilligung G (CH-DE).
Tools: PVSyst, AutoCAD, MS Office

HARD BLOCKERS — any of these auto-reject the role:
- Direct line management / Personalverantwortung over staff
- Physical installation or live electrical work on site
- French fluency required
- Swiss electrical trade certificate (EFZ) strictly required
- Student/retiree role or <20h/month
- Pure HV grid engineering (not OE/PM oversight)
"""

# Companies already rejected Soumi — skip automatically
REJECTED_COMPANIES = [
    "enshift", "gruner", "aventron", "primeo", "solarmarkt",
    "ewz", "bakerhicks", "agap2"
]

SEARCH_URLS = [
    "https://www.linkedin.com/jobs/search/?keywords=Projektleiter%20Photovoltaik&location=Switzerland&f_TPR=r604800&f_WT=2",
    "https://www.linkedin.com/jobs/search/?keywords=Projektleiter%20Solar%20PV&location=Switzerland&f_TPR=r604800&f_WT=2",
    "https://www.linkedin.com/jobs/search/?keywords=Projektentwickler%20Solar&location=Switzerland&f_TPR=r604800&f_WT=2",
    "https://www.linkedin.com/jobs/search/?keywords=Technical%20Project%20Manager%20Solar&location=Switzerland&f_TPR=r604800&f_WT=2",
    "https://www.linkedin.com/jobs/search/?keywords=Owner%20Engineer%20Renewable%20Energy&location=Switzerland&f_TPR=r604800&f_WT=2",
    "https://www.linkedin.com/jobs/search/?keywords=EPC%20Solar%20Project%20Manager&location=Switzerland&f_TPR=r604800&f_WT=2",
    "https://www.linkedin.com/jobs/search/?keywords=Projektleiter%20Solarpark&location=Switzerland&f_TPR=r604800&f_WT=2",
    "https://www.linkedin.com/jobs/search/?keywords=PV%20Projektmanager&location=Basel%2C%20Switzerland&f_TPR=r604800&f_WT=2",
    "https://www.linkedin.com/jobs/search/?keywords=Solar%20Project%20Manager&location=Zurich%2C%20Switzerland&f_TPR=r604800&f_WT=2",
    "https://www.linkedin.com/jobs/search/?keywords=Technischer%20Projektleiter%20Erneuerbare%20Energien&location=Switzerland&f_TPR=r604800&f_WT=2",
]

# ─── LINKEDIN SEARCH ─────────────────────────────────────────────────────────

def search_linkedin():
    print("Starting LinkedIn search via Apify...")

    # Start the actor run
    start_resp = requests.post(
        "https://api.apify.com/v2/acts/curious_coder~linkedin-jobs-scraper/runs",
        params={"token": APIFY_API_KEY},
        json={"urls": SEARCH_URLS, "count": 40, "scrapeCompany": False}
    )
    start_resp.raise_for_status()
    run_id = start_resp.json()["data"]["id"]
    print(f"Run started: {run_id}")

    # Poll until finished (max 3 minutes)
    for _ in range(18):
        time.sleep(10)
        status_resp = requests.get(
            f"https://api.apify.com/v2/acts/curious_coder~linkedin-jobs-scraper/runs/{run_id}",
            params={"token": APIFY_API_KEY}
        )
        status = status_resp.json()["data"]["status"]
        print(f"  Status: {status}")
        if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
            break

    if status != "SUCCEEDED":
        print(f"Run did not succeed: {status}")
        return []

    # Get dataset
    dataset_id = status_resp.json()["data"]["defaultDatasetId"]
    items_resp = requests.get(
        f"https://api.apify.com/v2/datasets/{dataset_id}/items",
        params={"token": APIFY_API_KEY, "clean": "true", "limit": 100}
    )
    items_resp.raise_for_status()
    jobs = items_resp.json()
    print(f"Retrieved {len(jobs)} LinkedIn jobs")
    return jobs

# ─── SCORING ─────────────────────────────────────────────────────────────────

def score_job(title, company, location, description):
    prompt = f"""You are evaluating job fit for this candidate:

{CV_PROFILE}

Job:
Title: {title}
Company: {company}
Location: {location}
Description: {description[:3000]}

Score fit 0–100 across these dimensions:
- Domain match (utility/rooftop PV = 25pts; other solar = 15pts; unrelated = 0): /25
- Function match (PM/OE/project delivery = 25pts; asset mgmt = 15pts; field install = 0): /25
- Language (German B1 ok = 20pts; B2 strictly required = 10pts; French required = 0): /20
- Seniority (mid-senior 5yr fit = 15pts; too junior/senior = 5pts): /15
- No hard blockers (none = 15pts; 1 blocker = 7pts; 2+ blockers = 0): /15

Hard blockers that cap total at 30 regardless of other scores:
- Direct line management / Personalverantwortung
- Physical installation or live electrical work
- French fluency required
- Swiss EFZ trade certificate strictly required

Reply ONLY with valid JSON, no other text:
{{"score": 72, "verdict": "Apply", "key_match": "PV EPC tendering, OE scope", "key_gap": "German B2 preferred"}}

verdict = "Apply" if score >= 50, else "Skip"."""

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        },
        json={
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 150,
            "messages": [{"role": "user", "content": prompt}]
        }
    )

    if resp.status_code == 200:
        try:
            text = resp.json()["content"][0]["text"].strip()
            # Strip markdown code fences if present
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            return json.loads(text.strip())
        except Exception as e:
            print(f"  Score parse error: {e}")
    return None

# ─── EMAIL ───────────────────────────────────────────────────────────────────

def send_email(matches):
    date_str = datetime.now().strftime("%d %B %Y")
    body = f"Weekly PV Job Alert — {date_str}\n"
    body += f"Found {len(matches)} role(s) matching your profile (≥50% fit):\n"
    body += "=" * 60 + "\n\n"

    for j in matches:
        body += f"ROLE:      {j['title']}\n"
        body += f"COMPANY:   {j['company']}\n"
        body += f"LOCATION:  {j['location']}\n"
        body += f"FIT:       {j['score']}%\n"
        body += f"MATCH:     {j['key_match']}\n"
        body += f"GAP:       {j['key_gap']}\n"
        body += f"LINK:      {j['link']}\n"
        body += "-" * 40 + "\n\n"

    msg = MIMEMultipart()
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = EMAIL_RECIPIENT
    msg["Subject"] = f"[PV Job Alert] {len(matches)} Match(es) Found — {date_str}"
    msg.attach(MIMEText(body, "plain"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.send_message(msg)

    print(f"Email sent: {len(matches)} match(es)")

# ─── MAIN ────────────────────────────────────────────────────────────────────

def is_rejected(company):
    return any(r in company.lower() for r in REJECTED_COMPANIES)

def main():
    print(f"\n{'='*60}")
    print(f"PV Job Search — {datetime.now().strftime('%d %B %Y %H:%M')}")
    print(f"{'='*60}\n")

    linkedin_jobs = search_linkedin()
    matches = []
    seen = set()

    for job in linkedin_jobs:
        title    = job.get("title", "")
        company  = job.get("companyName", "")
        location = job.get("location", "")
        desc     = job.get("descriptionText", "")
        link     = job.get("link", "")

        # Skip rejected companies
        if is_rejected(company):
            print(f"Skipping (rejected company): {company}")
            continue

        # Deduplicate
        key = f"{title}|{company}".lower()
        if key in seen:
            continue
        seen.add(key)

        print(f"Scoring: {title} @ {company} [{location}]")
        result = score_job(title, company, location, desc)

        if result:
            score = result.get("score", 0)
            print(f"  → {score}% — {result.get('verdict', 'Skip')}")
            if score >= 50:
                matches.append({
                    "title":     title,
                    "company":   company,
                    "location":  location,
                    "score":     score,
                    "key_match": result.get("key_match", ""),
                    "key_gap":   result.get("key_gap", ""),
                    "link":      link
                })
        else:
            print("  → Scoring failed, skipping")

        time.sleep(0.5)  # Rate limit buffer

    print(f"\nTotal matches: {len(matches)}")

    if matches:
        send_email(matches)
    else:
        print("No matches — no email sent.")

if __name__ == "__main__":
    main()
