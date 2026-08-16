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
import re
import json
import requests
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from urllib.parse import urlparse

from job_filters import (agency_flag, apply_filter_chain, fetch_jd_body, resolve_final_url,
                         german_prescreen_flag)
from swiss_employers import fetch_swiss_employer_jobs
from swiss_boards import fetch_swiss_board_jobs
from linkedin_apify import fetch_linkedin_jobs
from rejection_cooldowns import load_cooldowns, is_blocked, format_expiring_soon

# ─── SECRETS ────────────────────────────────────────────────────────────────
ADZUNA_APP_ID   = os.environ["ADZUNA_APP_ID"]
ADZUNA_APP_KEY  = os.environ["ADZUNA_APP_KEY"]
EMAIL_SENDER    = os.environ["EMAIL_SENDER"]
EMAIL_PASSWORD  = os.environ["EMAIL_PASSWORD"]
EMAIL_RECIPIENT = os.environ["EMAIL_RECIPIENT"]
# ─── ADZUNA SEEN-JOBS MEMORY ─────────────────────────────────────────────────
SEEN_ADZUNA_PATH = "seen_adzuna_jobs.json"

def load_seen_adzuna(path=SEEN_ADZUNA_PATH):
    try:
        with open(path) as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()

def save_seen_adzuna(seen, path=SEEN_ADZUNA_PATH):
    with open(path, "w") as f:
        json.dump(sorted(seen), f, indent=2)
# ─── SEARCH QUERIES ──────────────────────────────────────────────────────────
# 2026-07-17 sync with criteria doc v6: added owner-side/TDD/tender terms —
# the segment where the profile converts best.
# 2026-08-04: "Tender Manager Solar" removed. Criteria v9 + the 2026-07-29
# narrow-specialist rule reject tender/quotation/bid roles outright, so the
# scraper should not be spending a query slot hunting for them.
QUERIES = [
    "Projektleiter Photovoltaik",
    "Projektleiter Solar",
    "Technical Project Manager Solar",
    "EPC Solar Project Manager",
    "Owner Engineer Renewable Energy",
    "Projektleiter Solarpark",
    "PV Projektmanager",
    "Solar Projektmanager",
    "PV Project Manager",
    "Solar Project Manager",
    "Bauherrenvertretung Photovoltaik",
    "Technical Due Diligence Renewables",
    "Bauherrenberatung Erneuerbare Energien",
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

# 2026-08-04: HARD_BLOCKERS and DOMAIN_MISMATCH are now REGEX patterns, matched
# with re.search instead of naive substring containment. Substring matching had
# produced silent false positives on ordinary German text (see notes below).
# Literal entries are unchanged in meaning; only the matching mechanism and the
# few demonstrably broken entries were altered.

# 2026-07-17: added driving-licence blockers (no licence — field/Aussendienst
# roles are structurally out per criteria v6).
# 2026-08-04: "elektroinstallateur efz" removed from this list. The credential
# gate now lives in job_filters.passes_requirements_body, which honours
# softeners ("idealerweise", "von Vorteil", "oder Studium") per the
# pre-screen-before-withdraw rule of 2026-07-29 — vindicated on 2026-07-31 when
# tritec waived the EFZ requirement in writing. Keeping a hard duplicate here
# would have defeated that fix at scoring time.
HARD_BLOCKERS = [
    r"montage-elektriker",
    r"französisch zwingend",
    r"french mandatory",
    r"french fluent required",
    r"auf dächern",
    r"auf dem dach",
    r"\bpsa\b",                     # was "psa": unbounded substring
    r"dachdecker",
    r"\bmonteur",
    r"10-20 stunden",
    r"studentenjob",
    r"führerschein kat\. b",
    r"führerausweis kat\. b",
    r"führerschein der kategorie b",
]

DOMAIN_MISMATCH = [
    r"wasserkraft",
    r"hydro(?!gen)",                # hydropower, but not green-hydrogen asides
    r"\bwärme\b",                   # heat as the domain, not "Abwärme"/"Wärmepumpe"
    r"steam turbine",
    r"quantum",
    r"pharma",
    r"rolling stock",
    r"\bautomation\b",
    r"buchhaltung",
    r"accountant",
    r"\bhr\b",                      # 2026-08-04 CRITICAL FIX: was "hr ", which is a
                                    # substring of "Ihr ", "sehr ", "mehr " and "Jahr ".
                                    # Because this check returns score 0 / "Skip" before
                                    # any scoring, essentially every German-language JD
                                    # was discarded as a domain mismatch.
    r"informatik",
    r"netzelektriker",
    r"dachmonteur",
    r"solarteur",
]


def _host(url):
    """Short hostname for the JD-provenance line."""
    try:
        return re.sub(r"^www\.", "", urlparse(url).netloc) or "?"
    except Exception:
        return "?"


def _first_pattern_hit(patterns, text):
    """Return the first pattern that matches, else None."""
    return next((p for p in patterns if re.search(p, text)), None)


def score_job(title, company, description):
    text = (title + " " + description).lower()

    mismatch_hit = _first_pattern_hit(DOMAIN_MISMATCH, text)
    if mismatch_hit:
        return 0, "Skip", "Domain mismatch", f"Domain mismatch: {mismatch_hit}"

    blocker_hit = _first_pattern_hit(HARD_BLOCKERS, text)

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


def process_adzuna(raw_jobs, cooldowns, seen_ids):
    matches = []
    for job in raw_jobs:
        jid = job.get("id", "")
        if jid and jid in seen_ids:
            print(f"  SKIP (already seen): {job.get('title','')}")
            continue
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

        # Adzuna hands out tracking redirects — resolve to the employer's
        # real advert first, or the requirements stated only there are unseen.
        jd_url  = resolve_final_url(link) if link else ""
        jd_body = fetch_jd_body(jd_url) if jd_url else ""
        # Provenance note. Adzuna's hand-off to the employer's advert is done in
        # JavaScript (details -> /land/ad/<id>?aztt=<token>), so a runner cannot
        # reach the real posting: everything we see is Adzuna's own truncated
        # snippet. Say so, rather than let a clean filter pass imply the full
        # requirements were checked. (Verified 2026-08-11.)
        _h = _host(jd_url)
        if not jd_body:
            jd_note = "NOT RETRIEVED - requirements unverified"
        elif "adzuna" in _h:
            jd_note = (f"Adzuna snippet only ({len(jd_body)} chars) - full advert NOT "
                       f"reachable; verify language + credentials on the employer page")
        else:
            jd_note = f"{len(jd_body)} chars from {_h}"
        lang_note = german_prescreen_flag(jd_body)
        agency_note = agency_flag(company, jd_body)

        keep, reason = apply_filter_chain(
            title=title, location=loc, jd_body=jd_body,
            workmode="", short_description=desc, company=company,
        )
        if not keep:
            print(f"  FILTER ({reason}): {title}")
            continue

        score, verdict, key_match, key_gap = score_job(title, company, desc)
        print(f"  {verdict} ({score}%): {title} @ {company}")

        if verdict == "Apply":
            # 2026-08-04: only emailed roles are recorded as seen. Previously the
            # id was added here for every job that passed the filter chain, so a
            # near-miss (say 45%) was suppressed forever and never resurfaced —
            # exactly the band worth re-reading once criteria or wording change.
            if jid:
                seen_ids.add(jid)
            matches.append({
                "source": "adzuna",
                "title": title, "company": company, "location": loc,
                "score": score, "key_match": key_match, "key_gap": key_gap,
                "link": link, "jd_note": jd_note, "lang_note": lang_note,
                "agency_note": agency_note,
            })
    return matches


def filter_swiss_by_cooldown(swiss_jobs, cooldowns):
    """Cooldown filter + domain-relevance gate for Swiss scraper results.

    2026-08-11: these paths previously ran cooldown checks ONLY. score_job was
    applied to Adzuna results alone, so a Swiss-direct hit needed just a PM
    keyword in the title and no red flag in the body to be emailed — which is
    how an Axpo SAP/ERP role reached the digest. score_job already rejects it
    (DOMAIN_MISMATCH on 'automation', 0% and no PV token anywhere in the body);
    it simply was never consulted. Reusing it here adds no new policy.
    """
    kept = []
    for j in swiss_jobs:
        if is_rejected_permanent(j.get("company", "")):
            print(f"  SWISS SKIP (permanent): {j['company']}")
            continue
        blocked, entry = is_blocked(j.get("company", ""), j.get("title", ""), cooldowns)
        if blocked:
            print(f"  SWISS SKIP (cooldown until {entry['blocked_until']}): {j['title']}")
            continue
        score, verdict, _km, key_gap = score_job(
            j.get("title", ""), j.get("company", ""), j.get("jd_body", "") or "")
        if verdict != "Apply":
            print(f"  SWISS SKIP ({key_gap or 'insufficient PV/PM signal'}): {j['title']}")
            continue
        j["score"] = score
        j.pop("jd_body", None)   # bulky; not needed beyond this point
        kept.append(j)
    return kept


# ─── EMAIL ───────────────────────────────────────────────────────────────────

def send_email(adzuna_matches, swiss_matches, expiring_cooldowns,
               board_matches=None, linkedin_matches=None):
    board_matches = board_matches or []
    linkedin_matches = linkedin_matches or []
    total = (len(adzuna_matches) + len(swiss_matches)
             + len(board_matches) + len(linkedin_matches))
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
            body += f"JD BODY:   {j.get('jd_note', 'n/a')}\n"
            if j.get('lang_note'):
                body += f"LANGUAGE:  {j['lang_note']}\n"
            if j.get('agency_note'):
                body += f"AGENCY:    {j['agency_note']}\n"
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
            body += f"JD BODY:   {j.get('jd_note', 'n/a')}\n"
            if j.get('lang_note'):
                body += f"LANGUAGE:  {j['lang_note']}\n"
            if j.get('agency_note'):
                body += f"AGENCY:    {j['agency_note']}\n"
            body += f"LINK:      {j['url']}\n"
            body += "-" * 40 + "\n\n"

    if board_matches:
        body += f"\n### SWISS BOARDS ({len(board_matches)})\n\n"
        for j in board_matches:
            body += f"ROLE:      {j['title']}\n"
            body += f"COMPANY:   {j.get('company') or '(see posting)'}\n"
            body += f"LOCATION:  {j.get('location') or '(see posting)'}\n"
            if j.get("posted"):
                body += f"POSTED:    {j['posted']}\n"
            body += f"JD BODY:   {j.get('jd_note', 'n/a')}\n"
            if j.get('lang_note'):
                body += f"LANGUAGE:  {j['lang_note']}\n"
            if j.get('agency_note'):
                body += f"AGENCY:    {j['agency_note']}\n"
            body += f"SOURCE:    {j.get('source', 'swiss-board')}\n"
            body += f"LINK:      {j['url']}\n"
            body += "-" * 40 + "\n\n"

    if linkedin_matches:
        body += f"\n### LINKEDIN (APIFY) ({len(linkedin_matches)})\n\n"
        for j in linkedin_matches:
            body += f"ROLE:      {j['title']}\n"
            body += f"COMPANY:   {j.get('company') or '(see posting)'}\n"
            body += f"LOCATION:  {j.get('location') or '(see posting)'}\n"
            if j.get("posted"):
                body += f"POSTED:    {j['posted']}\n"
            if j.get("headcount"):
                body += f"HEADCOUNT: {j['headcount']}\n"
            body += f"JD BODY:   {j.get('jd_note', 'n/a')}\n"
            if j.get('lang_note'):
                body += f"LANGUAGE:  {j['lang_note']}\n"
            if j.get('agency_note'):
                body += f"AGENCY:    {j['agency_note']}\n"
            body += f"LINK:      {j['url']}\n"
            body += "-" * 40 + "\n\n"

    if (not adzuna_matches and not swiss_matches
            and not board_matches and not linkedin_matches):
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
    seen_adzuna = load_seen_adzuna()
    raw_jobs = search_adzuna()
    adzuna_matches = process_adzuna(raw_jobs, cooldowns, seen_adzuna)
    save_seen_adzuna(seen_adzuna)

    print("\n--- SWISS EMPLOYER DIRECT SCRAPE ---")
    try:
        swiss_raw = fetch_swiss_employer_jobs(state_path="seen_swiss_jobs.json")
        swiss_matches = filter_swiss_by_cooldown(swiss_raw, cooldowns)
    except Exception as e:
        print(f"[WARN] Swiss scrape failed: {e}")
        swiss_matches = []

    print("\n--- SWISS BOARD SCRAPE (Swissolar / JobScout24 / Fachplanung) ---")
    try:
        board_raw = fetch_swiss_board_jobs(state_path="seen_board_jobs.json")
        board_matches = filter_swiss_by_cooldown(board_raw, cooldowns)
    except Exception as e:
        print(f"[WARN] Swiss board scrape failed: {e}")
        board_matches = []

    print("\n--- LINKEDIN VIA APIFY ---")
    try:
        linkedin_raw = fetch_linkedin_jobs(state_path="seen_linkedin_jobs.json")
        linkedin_matches = filter_swiss_by_cooldown(linkedin_raw, cooldowns)
    except Exception as e:
        print(f"[WARN] LinkedIn/Apify scrape failed: {e}")
        linkedin_matches = []

    expiring = format_expiring_soon(cooldowns, days=30)

    print(f"\n--- SUMMARY ---")
    print(f"Adzuna matches: {len(adzuna_matches)}")
    print(f"Swiss direct matches: {len(swiss_matches)}")
    print(f"Swiss board matches: {len(board_matches)}")
    print(f"LinkedIn matches: {len(linkedin_matches)}")
    print(f"Cooldowns expiring soon: {len(expiring)}")

    if (adzuna_matches or swiss_matches or board_matches
            or linkedin_matches or expiring):
        send_email(adzuna_matches, swiss_matches, expiring, board_matches,
                   linkedin_matches)
    else:
        print("No matches and no expiring cooldowns — no email sent.")


if __name__ == "__main__":
    main()
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
import re
import json
import requests
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from urllib.parse import urlparse

from job_filters import (agency_flag, apply_filter_chain, fetch_jd_body, resolve_final_url,
                         german_prescreen_flag)
from swiss_employers import fetch_swiss_employer_jobs
from swiss_boards import fetch_swiss_board_jobs
from linkedin_apify import fetch_linkedin_jobs
from rejection_cooldowns import load_cooldowns, is_blocked, format_expiring_soon

# ─── SECRETS ────────────────────────────────────────────────────────────────
ADZUNA_APP_ID   = os.environ["ADZUNA_APP_ID"]
ADZUNA_APP_KEY  = os.environ["ADZUNA_APP_KEY"]
EMAIL_SENDER    = os.environ["EMAIL_SENDER"]
EMAIL_PASSWORD  = os.environ["EMAIL_PASSWORD"]
EMAIL_RECIPIENT = os.environ["EMAIL_RECIPIENT"]
# ─── ADZUNA SEEN-JOBS MEMORY ─────────────────────────────────────────────────
SEEN_ADZUNA_PATH = "seen_adzuna_jobs.json"

def load_seen_adzuna(path=SEEN_ADZUNA_PATH):
    try:
        with open(path) as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()

def save_seen_adzuna(seen, path=SEEN_ADZUNA_PATH):
    with open(path, "w") as f:
        json.dump(sorted(seen), f, indent=2)
# ─── SEARCH QUERIES ──────────────────────────────────────────────────────────
# 2026-07-17 sync with criteria doc v6: added owner-side/TDD/tender terms —
# the segment where the profile converts best.
# 2026-08-04: "Tender Manager Solar" removed. Criteria v9 + the 2026-07-29
# narrow-specialist rule reject tender/quotation/bid roles outright, so the
# scraper should not be spending a query slot hunting for them.
QUERIES = [
    "Projektleiter Photovoltaik",
    "Projektleiter Solar",
    "Technical Project Manager Solar",
    "EPC Solar Project Manager",
    "Owner Engineer Renewable Energy",
    "Projektleiter Solarpark",
    "PV Projektmanager",
    "Solar Projektmanager",
    "PV Project Manager",
    "Solar Project Manager",
    "Bauherrenvertretung Photovoltaik",
    "Technical Due Diligence Renewables",
    "Bauherrenberatung Erneuerbare Energien",
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

# 2026-08-04: HARD_BLOCKERS and DOMAIN_MISMATCH are now REGEX patterns, matched
# with re.search instead of naive substring containment. Substring matching had
# produced silent false positives on ordinary German text (see notes below).
# Literal entries are unchanged in meaning; only the matching mechanism and the
# few demonstrably broken entries were altered.

# 2026-07-17: added driving-licence blockers (no licence — field/Aussendienst
# roles are structurally out per criteria v6).
# 2026-08-04: "elektroinstallateur efz" removed from this list. The credential
# gate now lives in job_filters.passes_requirements_body, which honours
# softeners ("idealerweise", "von Vorteil", "oder Studium") per the
# pre-screen-before-withdraw rule of 2026-07-29 — vindicated on 2026-07-31 when
# tritec waived the EFZ requirement in writing. Keeping a hard duplicate here
# would have defeated that fix at scoring time.
HARD_BLOCKERS = [
    r"montage-elektriker",
    r"französisch zwingend",
    r"french mandatory",
    r"french fluent required",
    r"auf dächern",
    r"auf dem dach",
    r"\bpsa\b",                     # was "psa": unbounded substring
    r"dachdecker",
    r"\bmonteur",
    r"10-20 stunden",
    r"studentenjob",
    r"führerschein kat\. b",
    r"führerausweis kat\. b",
    r"führerschein der kategorie b",
]

DOMAIN_MISMATCH = [
    r"wasserkraft",
    r"hydro(?!gen)",                # hydropower, but not green-hydrogen asides
    r"\bwärme\b",                   # heat as the domain, not "Abwärme"/"Wärmepumpe"
    r"steam turbine",
    r"quantum",
    r"pharma",
    r"rolling stock",
    r"\bautomation\b",
    r"buchhaltung",
    r"accountant",
    r"\bhr\b",                      # 2026-08-04 CRITICAL FIX: was "hr ", which is a
                                    # substring of "Ihr ", "sehr ", "mehr " and "Jahr ".
                                    # Because this check returns score 0 / "Skip" before
                                    # any scoring, essentially every German-language JD
                                    # was discarded as a domain mismatch.
    r"informatik",
    r"netzelektriker",
    r"dachmonteur",
    r"solarteur",
]


def _host(url):
    """Short hostname for the JD-provenance line."""
    try:
        return re.sub(r"^www\.", "", urlparse(url).netloc) or "?"
    except Exception:
        return "?"


def _first_pattern_hit(patterns, text):
    """Return the first pattern that matches, else None."""
    return next((p for p in patterns if re.search(p, text)), None)


def score_job(title, company, description):
    text = (title + " " + description).lower()

    mismatch_hit = _first_pattern_hit(DOMAIN_MISMATCH, text)
    if mismatch_hit:
        return 0, "Skip", "Domain mismatch", f"Domain mismatch: {mismatch_hit}"

    blocker_hit = _first_pattern_hit(HARD_BLOCKERS, text)

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


def process_adzuna(raw_jobs, cooldowns, seen_ids):
    matches = []
    for job in raw_jobs:
        jid = job.get("id", "")
        if jid and jid in seen_ids:
            print(f"  SKIP (already seen): {job.get('title','')}")
            continue
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

        # Adzuna hands out tracking redirects — resolve to the employer's
        # real advert first, or the requirements stated only there are unseen.
        jd_url  = resolve_final_url(link) if link else ""
        jd_body = fetch_jd_body(jd_url) if jd_url else ""
        # Provenance note. Adzuna's hand-off to the employer's advert is done in
        # JavaScript (details -> /land/ad/<id>?aztt=<token>), so a runner cannot
        # reach the real posting: everything we see is Adzuna's own truncated
        # snippet. Say so, rather than let a clean filter pass imply the full
        # requirements were checked. (Verified 2026-08-11.)
        _h = _host(jd_url)
        if not jd_body:
            jd_note = "NOT RETRIEVED - requirements unverified"
        elif "adzuna" in _h:
            jd_note = (f"Adzuna snippet only ({len(jd_body)} chars) - full advert NOT "
                       f"reachable; verify language + credentials on the employer page")
        else:
            jd_note = f"{len(jd_body)} chars from {_h}"
        lang_note = german_prescreen_flag(jd_body)
        agency_note = agency_flag(company, jd_body)

        keep, reason = apply_filter_chain(
            title=title, location=loc, jd_body=jd_body,
            workmode="", short_description=desc, company=company,
        )
        if not keep:
            print(f"  FILTER ({reason}): {title}")
            continue

        score, verdict, key_match, key_gap = score_job(title, company, desc)
        print(f"  {verdict} ({score}%): {title} @ {company}")

        if verdict == "Apply":
            # 2026-08-04: only emailed roles are recorded as seen. Previously the
            # id was added here for every job that passed the filter chain, so a
            # near-miss (say 45%) was suppressed forever and never resurfaced —
            # exactly the band worth re-reading once criteria or wording change.
            if jid:
                seen_ids.add(jid)
            matches.append({
                "source": "adzuna",
                "title": title, "company": company, "location": loc,
                "score": score, "key_match": key_match, "key_gap": key_gap,
                "link": link, "jd_note": jd_note, "lang_note": lang_note,
                "agency_note": agency_note,
            })
    return matches


def filter_swiss_by_cooldown(swiss_jobs, cooldowns):
    """Cooldown filter + domain-relevance gate for Swiss scraper results.

    2026-08-11: these paths previously ran cooldown checks ONLY. score_job was
    applied to Adzuna results alone, so a Swiss-direct hit needed just a PM
    keyword in the title and no red flag in the body to be emailed — which is
    how an Axpo SAP/ERP role reached the digest. score_job already rejects it
    (DOMAIN_MISMATCH on 'automation', 0% and no PV token anywhere in the body);
    it simply was never consulted. Reusing it here adds no new policy.
    """
    kept = []
    for j in swiss_jobs:
        if is_rejected_permanent(j.get("company", "")):
            print(f"  SWISS SKIP (permanent): {j['company']}")
            continue
        blocked, entry = is_blocked(j.get("company", ""), j.get("title", ""), cooldowns)
        if blocked:
            print(f"  SWISS SKIP (cooldown until {entry['blocked_until']}): {j['title']}")
            continue
        score, verdict, _km, key_gap = score_job(
            j.get("title", ""), j.get("company", ""), j.get("jd_body", "") or "")
        if verdict != "Apply":
            print(f"  SWISS SKIP ({key_gap or 'insufficient PV/PM signal'}): {j['title']}")
            continue
        j["score"] = score
        j.pop("jd_body", None)   # bulky; not needed beyond this point
        kept.append(j)
    return kept


# ─── EMAIL ───────────────────────────────────────────────────────────────────

def send_email(adzuna_matches, swiss_matches, expiring_cooldowns,
               board_matches=None, linkedin_matches=None):
    board_matches = board_matches or []
    linkedin_matches = linkedin_matches or []
    total = (len(adzuna_matches) + len(swiss_matches)
             + len(board_matches) + len(linkedin_matches))
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
            body += f"JD BODY:   {j.get('jd_note', 'n/a')}\n"
            if j.get('lang_note'):
                body += f"LANGUAGE:  {j['lang_note']}\n"
            if j.get('agency_note'):
                body += f"AGENCY:    {j['agency_note']}\n"
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
            body += f"JD BODY:   {j.get('jd_note', 'n/a')}\n"
            if j.get('lang_note'):
                body += f"LANGUAGE:  {j['lang_note']}\n"
            if j.get('agency_note'):
                body += f"AGENCY:    {j['agency_note']}\n"
            body += f"LINK:      {j['url']}\n"
            body += "-" * 40 + "\n\n"

    if board_matches:
        body += f"\n### SWISS BOARDS ({len(board_matches)})\n\n"
        for j in board_matches:
            body += f"ROLE:      {j['title']}\n"
            body += f"COMPANY:   {j.get('company') or '(see posting)'}\n"
            body += f"LOCATION:  {j.get('location') or '(see posting)'}\n"
            if j.get("posted"):
                body += f"POSTED:    {j['posted']}\n"
            body += f"JD BODY:   {j.get('jd_note', 'n/a')}\n"
            if j.get('lang_note'):
                body += f"LANGUAGE:  {j['lang_note']}\n"
            if j.get('agency_note'):
                body += f"AGENCY:    {j['agency_note']}\n"
            body += f"SOURCE:    {j.get('source', 'swiss-board')}\n"
            body += f"LINK:      {j['url']}\n"
            body += "-" * 40 + "\n\n"

    if linkedin_matches:
        body += f"\n### LINKEDIN (APIFY) ({len(linkedin_matches)})\n\n"
        for j in linkedin_matches:
            body += f"ROLE:      {j['title']}\n"
            body += f"COMPANY:   {j.get('company') or '(see posting)'}\n"
            body += f"LOCATION:  {j.get('location') or '(see posting)'}\n"
            if j.get("posted"):
                body += f"POSTED:    {j['posted']}\n"
            if j.get("headcount"):
                body += f"HEADCOUNT: {j['headcount']}\n"
            body += f"JD BODY:   {j.get('jd_note', 'n/a')}\n"
            if j.get('lang_note'):
                body += f"LANGUAGE:  {j['lang_note']}\n"
            if j.get('agency_note'):
                body += f"AGENCY:    {j['agency_note']}\n"
            body += f"LINK:      {j['url']}\n"
            body += "-" * 40 + "\n\n"

    if (not adzuna_matches and not swiss_matches
            and not board_matches and not linkedin_matches):
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
    seen_adzuna = load_seen_adzuna()
    raw_jobs = search_adzuna()
    adzuna_matches = process_adzuna(raw_jobs, cooldowns, seen_adzuna)
    save_seen_adzuna(seen_adzuna)

    print("\n--- SWISS EMPLOYER DIRECT SCRAPE ---")
    try:
        swiss_raw = fetch_swiss_employer_jobs(state_path="seen_swiss_jobs.json")
        swiss_matches = filter_swiss_by_cooldown(swiss_raw, cooldowns)
    except Exception as e:
        print(f"[WARN] Swiss scrape failed: {e}")
        swiss_matches = []

    print("\n--- SWISS BOARD SCRAPE (Swissolar / JobScout24 / Fachplanung) ---")
    try:
        board_raw = fetch_swiss_board_jobs(state_path="seen_board_jobs.json")
        board_matches = filter_swiss_by_cooldown(board_raw, cooldowns)
    except Exception as e:
        print(f"[WARN] Swiss board scrape failed: {e}")
        board_matches = []

    print("\n--- LINKEDIN VIA APIFY ---")
    try:
        linkedin_raw = fetch_linkedin_jobs(state_path="seen_linkedin_jobs.json")
Wire LinkedIn/Apify source into the pipeline    except Exception as e:
        print(f"[WARN] LinkedIn/Apify scrape failed: {e}")
        linkedin_matches = []

    expiring = format_expiring_soon(cooldowns, days=30)

    print(f"\n--- SUMMARY ---")
    print(f"Adzuna matches: {len(adzuna_matches)}")Adds fetch_linkedin_jobs to main(), a LINKEDIN (APIFY) digest section, and the summary/dispatch counts. Results pass through filter_swiss_by_cooldown, so the existing cooldown check and score_job gate apply unchanged - no new scoring policy. Failure is caught and logged; the other three sources still send.
    print(f"Swiss direct matches: {len(swiss_matches)}")
    print(f"Swiss board matches: {len(board_matches)}")
    print(f"LinkedIn matches: {len(linkedin_matches)}")
    print(f"Cooldowns expiring soon: {len(expiring)}")

    if (adzuna_matches or swiss_matches or board_matches
            or linkedin_matches or expiring):
        send_email(adzuna_matches, swiss_matches, expiring, board_matches,
                   linkedin_matches)
    else:
        print("No matches and no expiring cooldowns — no email sent.")"""
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
import re
import json
import requests
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from urllib.parse import urlparse

from job_filters import (agency_flag, apply_filter_chain, fetch_jd_body, resolve_final_url,
                         german_prescreen_flag)
from swiss_employers import fetch_swiss_employer_jobs
from swiss_boards import fetch_swiss_board_jobs
from linkedin_apify import fetch_linkedin_jobs
from rejection_cooldowns import load_cooldowns, is_blocked, format_expiring_soon

# ─── SECRETS ────────────────────────────────────────────────────────────────
ADZUNA_APP_ID   = os.environ["ADZUNA_APP_ID"]
ADZUNA_APP_KEY  = os.environ["ADZUNA_APP_KEY"]
EMAIL_SENDER    = os.environ["EMAIL_SENDER"]
EMAIL_PASSWORD  = os.environ["EMAIL_PASSWORD"]
EMAIL_RECIPIENT = os.environ["EMAIL_RECIPIENT"]
# ─── ADZUNA SEEN-JOBS MEMORY ─────────────────────────────────────────────────
SEEN_ADZUNA_PATH = "seen_adzuna_jobs.json"

def load_seen_adzuna(path=SEEN_ADZUNA_PATH):
    try:
        with open(path) as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()

def save_seen_adzuna(seen, path=SEEN_ADZUNA_PATH):
    with open(path, "w") as f:
        json.dump(sorted(seen), f, indent=2)
# ─── SEARCH QUERIES ──────────────────────────────────────────────────────────
# 2026-07-17 sync with criteria doc v6: added owner-side/TDD/tender terms —
# the segment where the profile converts best.
# 2026-08-04: "Tender Manager Solar" removed. Criteria v9 + the 2026-07-29
# narrow-specialist rule reject tender/quotation/bid roles outright, so the
# scraper should not be spending a query slot hunting for them.
QUERIES = [
    "Projektleiter Photovoltaik",
    "Projektleiter Solar",
    "Technical Project Manager Solar",
    "EPC Solar Project Manager",
    "Owner Engineer Renewable Energy",
    "Projektleiter Solarpark",
    "PV Projektmanager",
    "Solar Projektmanager",
    "PV Project Manager",
    "Solar Project Manager",
    "Bauherrenvertretung Photovoltaik",
    "Technical Due Diligence Renewables",
    "Bauherrenberatung Erneuerbare Energien",
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

# 2026-08-04: HARD_BLOCKERS and DOMAIN_MISMATCH are now REGEX patterns, matched
# with re.search instead of naive substring containment. Substring matching had
# produced silent false positives on ordinary German text (see notes below).
# Literal entries are unchanged in meaning; only the matching mechanism and the
# few demonstrably broken entries were altered.

# 2026-07-17: added driving-licence blockers (no licence — field/Aussendienst
# roles are structurally out per criteria v6).
# 2026-08-04: "elektroinstallateur efz" removed from this list. The credential
# gate now lives in job_filters.passes_requirements_body, which honours
# softeners ("idealerweise", "von Vorteil", "oder Studium") per the
# pre-screen-before-withdraw rule of 2026-07-29 — vindicated on 2026-07-31 when
# tritec waived the EFZ requirement in writing. Keeping a hard duplicate here
# would have defeated that fix at scoring time.
HARD_BLOCKERS = [
    r"montage-elektriker",
    r"französisch zwingend",
    r"french mandatory",
    r"french fluent required",
    r"auf dächern",
    r"auf dem dach",
    r"\bpsa\b",                     # was "psa": unbounded substring
    r"dachdecker",
    r"\bmonteur",
    r"10-20 stunden",
    r"studentenjob",
    r"führerschein kat\. b",
    r"führerausweis kat\. b",
    r"führerschein der kategorie b",
]

DOMAIN_MISMATCH = [
    r"wasserkraft",
    r"hydro(?!gen)",                # hydropower, but not green-hydrogen asides
    r"\bwärme\b",                   # heat as the domain, not "Abwärme"/"Wärmepumpe"
    r"steam turbine",
    r"quantum",
    r"pharma",
    r"rolling stock",
    r"\bautomation\b",
    r"buchhaltung",
    r"accountant",
    r"\bhr\b",                      # 2026-08-04 CRITICAL FIX: was "hr ", which is a
                                    # substring of "Ihr ", "sehr ", "mehr " and "Jahr ".
                                    # Because this check returns score 0 / "Skip" before
                                    # any scoring, essentially every German-language JD
                                    # was discarded as a domain mismatch.
    r"informatik",
    r"netzelektriker",
    r"dachmonteur",
    r"solarteur",
]


def _host(url):
    """Short hostname for the JD-provenance line."""
    try:
        return re.sub(r"^www\.", "", urlparse(url).netloc) or "?"
    except Exception:
        return "?"


def _first_pattern_hit(patterns, text):
    """Return the first pattern that matches, else None."""
    return next((p for p in patterns if re.search(p, text)), None)


def score_job(title, company, description):
    text = (title + " " + description).lower()

    mismatch_hit = _first_pattern_hit(DOMAIN_MISMATCH, text)
    if mismatch_hit:
        return 0, "Skip", "Domain mismatch", f"Domain mismatch: {mismatch_hit}"

    blocker_hit = _first_pattern_hit(HARD_BLOCKERS, text)

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


def process_adzuna(raw_jobs, cooldowns, seen_ids):
    matches = []
    for job in raw_jobs:
        jid = job.get("id", "")
        if jid and jid in seen_ids:
            print(f"  SKIP (already seen): {job.get('title','')}")
            continue
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

        # Adzuna hands out tracking redirects — resolve to the employer's
        # real advert first, or the requirements stated only there are unseen.
        jd_url  = resolve_final_url(link) if link else ""
        jd_body = fetch_jd_body(jd_url) if jd_url else ""
        # Provenance note. Adzuna's hand-off to the employer's advert is done in
        # JavaScript (details -> /land/ad/<id>?aztt=<token>), so a runner cannot
        # reach the real posting: everything we see is Adzuna's own truncated
        # snippet. Say so, rather than let a clean filter pass imply the full
        # requirements were checked. (Verified 2026-08-11.)
        _h = _host(jd_url)
        if not jd_body:
            jd_note = "NOT RETRIEVED - requirements unverified"
        elif "adzuna" in _h:
            jd_note = (f"Adzuna snippet only ({len(jd_body)} chars) - full advert NOT "
                       f"reachable; verify language + credentials on the employer page")
        else:
            jd_note = f"{len(jd_body)} chars from {_h}"
        lang_note = german_prescreen_flag(jd_body)
        agency_note = agency_flag(company, jd_body)

        keep, reason = apply_filter_chain(
            title=title, location=loc, jd_body=jd_body,
            workmode="", short_description=desc, company=company,
        )
        if not keep:
            print(f"  FILTER ({reason}): {title}")
            continue

        score, verdict, key_match, key_gap = score_job(title, company, desc)
        print(f"  {verdict} ({score}%): {title} @ {company}")

        if verdict == "Apply":
            # 2026-08-04: only emailed roles are recorded as seen. Previously the
            # id was added here for every job that passed the filter chain, so a
            # near-miss (say 45%) was suppressed forever and never resurfaced —
            # exactly the band worth re-reading once criteria or wording change.
            if jid:
                seen_ids.add(jid)
            matches.append({
                "source": "adzuna",
                "title": title, "company": company, "location": loc,
                "score": score, "key_match": key_match, "key_gap": key_gap,
                "link": link, "jd_note": jd_note, "lang_note": lang_note,
                "agency_note": agency_note,
            })
    return matches


def filter_swiss_by_cooldown(swiss_jobs, cooldowns):
    """Cooldown filter + domain-relevance gate for Swiss scraper results.

    2026-08-11: these paths previously ran cooldown checks ONLY. score_job was
    applied to Adzuna results alone, so a Swiss-direct hit needed just a PM
    keyword in the title and no red flag in the body to be emailed — which is
    how an Axpo SAP/ERP role reached the digest. score_job already rejects it
    (DOMAIN_MISMATCH on 'automation', 0% and no PV token anywhere in the body);
    it simply was never consulted. Reusing it here adds no new policy.
    """
    kept = []
    for j in swiss_jobs:
        if is_rejected_permanent(j.get("company", "")):
            print(f"  SWISS SKIP (permanent): {j['company']}")
            continue
        blocked, entry = is_blocked(j.get("company", ""), j.get("title", ""), cooldowns)
        if blocked:
            print(f"  SWISS SKIP (cooldown until {entry['blocked_until']}): {j['title']}")
            continue
        score, verdict, _km, key_gap = score_job(
            j.get("title", ""), j.get("company", ""), j.get("jd_body", "") or "")
        if verdict != "Apply":
            print(f"  SWISS SKIP ({key_gap or 'insufficient PV/PM signal'}): {j['title']}")
            continue
        j["score"] = score
        j.pop("jd_body", None)   # bulky; not needed beyond this point
        kept.append(j)
    return kept


# ─── EMAIL ───────────────────────────────────────────────────────────────────

def send_email(adzuna_matches, swiss_matches, expiring_cooldowns,
               board_matches=None, linkedin_matches=None):
    board_matches = board_matches or []
    linkedin_matches = linkedin_matches or []
    total = (len(adzuna_matches) + len(swiss_matches)
             + len(board_matches) + len(linkedin_matches))
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
            body += f"JD BODY:   {j.get('jd_note', 'n/a')}\n"
            if j.get('lang_note'):
                body += f"LANGUAGE:  {j['lang_note']}\n"
            if j.get('agency_note'):
                body += f"AGENCY:    {j['agency_note']}\n"
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
            body += f"JD BODY:   {j.get('jd_note', 'n/a')}\n"
            if j.get('lang_note'):
                body += f"LANGUAGE:  {j['lang_note']}\n"
            if j.get('agency_note'):
                body += f"AGENCY:    {j['agency_note']}\n"
            body += f"LINK:      {j['url']}\n"
            body += "-" * 40 + "\n\n"

    if board_matches:
        body += f"\n### SWISS BOARDS ({len(board_matches)})\n\n"
        for j in board_matches:
            body += f"ROLE:      {j['title']}\n"
            body += f"COMPANY:   {j.get('company') or '(see posting)'}\n"
            body += f"LOCATION:  {j.get('location') or '(see posting)'}\n"
            if j.get("posted"):
                body += f"POSTED:    {j['posted']}\n"
            body += f"JD BODY:   {j.get('jd_note', 'n/a')}\n"
            if j.get('lang_note'):
                body += f"LANGUAGE:  {j['lang_note']}\n"
            if j.get('agency_note'):
                body += f"AGENCY:    {j['agency_note']}\n"
            body += f"SOURCE:    {j.get('source', 'swiss-board')}\n"
            body += f"LINK:      {j['url']}\n"
            body += "-" * 40 + "\n\n"

    if linkedin_matches:
        body += f"\n### LINKEDIN (APIFY) ({len(linkedin_matches)})\n\n"
        for j in linkedin_matches:
            body += f"ROLE:      {j['title']}\n"
            body += f"COMPANY:   {j.get('company') or '(see posting)'}\n"
            body += f"LOCATION:  {j.get('location') or '(see posting)'}\n"
            if j.get("posted"):
                body += f"POSTED:    {j['posted']}\n"
            if j.get("headcount"):
                body += f"HEADCOUNT: {j['headcount']}\n"
            body += f"JD BODY:   {j.get('jd_note', 'n/a')}\n"
            if j.get('lang_note'):
                body += f"LANGUAGE:  {j['lang_note']}\n"
            if j.get('agency_note'):
                body += f"AGENCY:    {j['agency_note']}\n"
            body += f"LINK:      {j['url']}\n"
            body += "-" * 40 + "\n\n"

    if (not adzuna_matches and not swiss_matches
            and not board_matches and not linkedin_matches):
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
    seen_adzuna = load_seen_adzuna()
    raw_jobs = search_adzuna()
    adzuna_matches = process_adzuna(raw_jobs, cooldowns, seen_adzuna)
    save_seen_adzuna(seen_adzuna)

    print("\n--- SWISS EMPLOYER DIRECT SCRAPE ---")
    try:
        swiss_raw = fetch_swiss_employer_jobs(state_path="seen_swiss_jobs.json")
        swiss_matches = filter_swiss_by_cooldown(swiss_raw, cooldowns)
    except Exception as e:
        print(f"[WARN] Swiss scrape failed: {e}")
        swiss_matches = []

    print("\n--- SWISS BOARD SCRAPE (Swissolar / JobScout24 / Fachplanung) ---")
    try:
        board_raw = fetch_swiss_board_jobs(state_path="seen_board_jobs.json")
        board_matches = filter_swiss_by_cooldown(board_raw, cooldowns)
    except Exception as e:
        print(f"[WARN] Swiss board scrape failed: {e}")
        board_matches = []

    print("\n--- LINKEDIN VIA APIFY ---")
    try:
        linkedin_raw = fetch_linkedin_jobs(state_path="seen_linkedin_jobs.json")
        linkedin_matches = filter_swiss_by_cooldown(linkedin_raw, cooldowns)
    except Exception as e:
        print(f"[WARN] LinkedIn/Apify scrape failed: {e}")
        linkedin_matches = []

    expiring = format_expiring_soon(cooldowns, days=30)

    print(f"\n--- SUMMARY ---")
    print(f"Adzuna matches: {len(adzuna_matches)}")
    print(f"Swiss direct matches: {len(swiss_matches)}")
    print(f"Swiss board matches: {len(board_matches)}")
    print(f"LinkedIn matches: {len(linkedin_matches)}")
    print(f"Cooldowns expiring soon: {len(expiring)}")

    if (adzuna_matches or swiss_matches or board_matches
            or linkedin_matches or expiring):
        send_email(adzuna_matches, swiss_matches, expiring, board_matches,
                   linkedin_matches)
    else:
        print("No matches and no expiring cooldowns — no email sent.")


if __name__ == "__main__":
    main()



if __name__ == "__main__":
    main()
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
import re
import json
import requests
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from urllib.parse import urlparse

from job_filters import (agency_flag, apply_filter_chain, fetch_jd_body, resolve_final_url,
                         german_prescreen_flag)
from swiss_employers import fetch_swiss_employer_jobs
from swiss_boards import fetch_swiss_board_jobs
from rejection_cooldowns import load_cooldowns, is_blocked, format_expiring_soon

# ─── SECRETS ────────────────────────────────────────────────────────────────
ADZUNA_APP_ID   = os.environ["ADZUNA_APP_ID"]
ADZUNA_APP_KEY  = os.environ["ADZUNA_APP_KEY"]
EMAIL_SENDER    = os.environ["EMAIL_SENDER"]
EMAIL_PASSWORD  = os.environ["EMAIL_PASSWORD"]
EMAIL_RECIPIENT = os.environ["EMAIL_RECIPIENT"]
# ─── ADZUNA SEEN-JOBS MEMORY ─────────────────────────────────────────────────
SEEN_ADZUNA_PATH = "seen_adzuna_jobs.json"

def load_seen_adzuna(path=SEEN_ADZUNA_PATH):
    try:
        with open(path) as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()

def save_seen_adzuna(seen, path=SEEN_ADZUNA_PATH):
    with open(path, "w") as f:
        json.dump(sorted(seen), f, indent=2)
# ─── SEARCH QUERIES ──────────────────────────────────────────────────────────
# 2026-07-17 sync with criteria doc v6: added owner-side/TDD/tender terms —
# the segment where the profile converts best.
# 2026-08-04: "Tender Manager Solar" removed. Criteria v9 + the 2026-07-29
# narrow-specialist rule reject tender/quotation/bid roles outright, so the
# scraper should not be spending a query slot hunting for them.
QUERIES = [
    "Projektleiter Photovoltaik",
    "Projektleiter Solar",
    "Technical Project Manager Solar",
    "EPC Solar Project Manager",
    "Owner Engineer Renewable Energy",
    "Projektleiter Solarpark",
    "PV Projektmanager",
    "Solar Projektmanager",
    "PV Project Manager",
    "Solar Project Manager",
    "Bauherrenvertretung Photovoltaik",
    "Technical Due Diligence Renewables",
    "Bauherrenberatung Erneuerbare Energien",
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

# 2026-08-04: HARD_BLOCKERS and DOMAIN_MISMATCH are now REGEX patterns, matched
# with re.search instead of naive substring containment. Substring matching had
# produced silent false positives on ordinary German text (see notes below).
# Literal entries are unchanged in meaning; only the matching mechanism and the
# few demonstrably broken entries were altered.

# 2026-07-17: added driving-licence blockers (no licence — field/Aussendienst
# roles are structurally out per criteria v6).
# 2026-08-04: "elektroinstallateur efz" removed from this list. The credential
# gate now lives in job_filters.passes_requirements_body, which honours
# softeners ("idealerweise", "von Vorteil", "oder Studium") per the
# pre-screen-before-withdraw rule of 2026-07-29 — vindicated on 2026-07-31 when
# tritec waived the EFZ requirement in writing. Keeping a hard duplicate here
# would have defeated that fix at scoring time.
HARD_BLOCKERS = [
    r"montage-elektriker",
    r"französisch zwingend",
    r"french mandatory",
    r"french fluent required",
    r"auf dächern",
    r"auf dem dach",
    r"\bpsa\b",                     # was "psa": unbounded substring
    r"dachdecker",
    r"\bmonteur",
    r"10-20 stunden",
    r"studentenjob",
    r"führerschein kat\. b",
    r"führerausweis kat\. b",
    r"führerschein der kategorie b",
]

DOMAIN_MISMATCH = [
    r"wasserkraft",
    r"hydro(?!gen)",                # hydropower, but not green-hydrogen asides
    r"\bwärme\b",                   # heat as the domain, not "Abwärme"/"Wärmepumpe"
    r"steam turbine",
    r"quantum",
    r"pharma",
    r"rolling stock",
    r"\bautomation\b",
    r"buchhaltung",
    r"accountant",
    r"\bhr\b",                      # 2026-08-04 CRITICAL FIX: was "hr ", which is a
                                    # substring of "Ihr ", "sehr ", "mehr " and "Jahr ".
                                    # Because this check returns score 0 / "Skip" before
                                    # any scoring, essentially every German-language JD
                                    # was discarded as a domain mismatch.
    r"informatik",
    r"netzelektriker",
    r"dachmonteur",
    r"solarteur",
]


def _host(url):
    """Short hostname for the JD-provenance line."""
    try:
        return re.sub(r"^www\.", "", urlparse(url).netloc) or "?"
    except Exception:
        return "?"


def _first_pattern_hit(patterns, text):
    """Return the first pattern that matches, else None."""
    return next((p for p in patterns if re.search(p, text)), None)


def score_job(title, company, description):
    text = (title + " " + description).lower()

    mismatch_hit = _first_pattern_hit(DOMAIN_MISMATCH, text)
    if mismatch_hit:
        return 0, "Skip", "Domain mismatch", f"Domain mismatch: {mismatch_hit}"

    blocker_hit = _first_pattern_hit(HARD_BLOCKERS, text)

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


def process_adzuna(raw_jobs, cooldowns, seen_ids):
    matches = []
    for job in raw_jobs:
        jid = job.get("id", "")
        if jid and jid in seen_ids:
            print(f"  SKIP (already seen): {job.get('title','')}")
            continue
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

        # Adzuna hands out tracking redirects — resolve to the employer's
        # real advert first, or the requirements stated only there are unseen.
        jd_url  = resolve_final_url(link) if link else ""
        jd_body = fetch_jd_body(jd_url) if jd_url else ""
        # Provenance note. Adzuna's hand-off to the employer's advert is done in
        # JavaScript (details -> /land/ad/<id>?aztt=<token>), so a runner cannot
        # reach the real posting: everything we see is Adzuna's own truncated
        # snippet. Say so, rather than let a clean filter pass imply the full
        # requirements were checked. (Verified 2026-08-11.)
        _h = _host(jd_url)
        if not jd_body:
            jd_note = "NOT RETRIEVED - requirements unverified"
        elif "adzuna" in _h:
            jd_note = (f"Adzuna snippet only ({len(jd_body)} chars) - full advert NOT "
                       f"reachable; verify language + credentials on the employer page")
        else:
            jd_note = f"{len(jd_body)} chars from {_h}"
        lang_note = german_prescreen_flag(jd_body)
        agency_note = agency_flag(company, jd_body)

        keep, reason = apply_filter_chain(
            title=title, location=loc, jd_body=jd_body,
            workmode="", short_description=desc, company=company,
        )
        if not keep:
            print(f"  FILTER ({reason}): {title}")
            continue

        score, verdict, key_match, key_gap = score_job(title, company, desc)
        print(f"  {verdict} ({score}%): {title} @ {company}")

        if verdict == "Apply":
            # 2026-08-04: only emailed roles are recorded as seen. Previously the
            # id was added here for every job that passed the filter chain, so a
            # near-miss (say 45%) was suppressed forever and never resurfaced —
            # exactly the band worth re-reading once criteria or wording change.
            if jid:
                seen_ids.add(jid)
            matches.append({
                "source": "adzuna",
                "title": title, "company": company, "location": loc,
                "score": score, "key_match": key_match, "key_gap": key_gap,
                "link": link, "jd_note": jd_note, "lang_note": lang_note,
                "agency_note": agency_note,
            })
    return matches


def filter_swiss_by_cooldown(swiss_jobs, cooldowns):
    """Cooldown filter + domain-relevance gate for Swiss scraper results.

    2026-08-11: these paths previously ran cooldown checks ONLY. score_job was
    applied to Adzuna results alone, so a Swiss-direct hit needed just a PM
    keyword in the title and no red flag in the body to be emailed — which is
    how an Axpo SAP/ERP role reached the digest. score_job already rejects it
    (DOMAIN_MISMATCH on 'automation', 0% and no PV token anywhere in the body);
    it simply was never consulted. Reusing it here adds no new policy.
    """
    kept = []
    for j in swiss_jobs:
        if is_rejected_permanent(j.get("company", "")):
            print(f"  SWISS SKIP (permanent): {j['company']}")
            continue
        blocked, entry = is_blocked(j.get("company", ""), j.get("title", ""), cooldowns)
        if blocked:
            print(f"  SWISS SKIP (cooldown until {entry['blocked_until']}): {j['title']}")
            continue
        score, verdict, _km, key_gap = score_job(
            j.get("title", ""), j.get("company", ""), j.get("jd_body", "") or "")
        if verdict != "Apply":
            print(f"  SWISS SKIP ({key_gap or 'insufficient PV/PM signal'}): {j['title']}")
            continue
        j["score"] = score
        j.pop("jd_body", None)   # bulky; not needed beyond this point
        kept.append(j)
    return kept


# ─── EMAIL ───────────────────────────────────────────────────────────────────

def send_email(adzuna_matches, swiss_matches, expiring_cooldowns, board_matches=None):
    board_matches = board_matches or []
    total = len(adzuna_matches) + len(swiss_matches) + len(board_matches)
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
            body += f"JD BODY:   {j.get('jd_note', 'n/a')}\n"
            if j.get('lang_note'):
                body += f"LANGUAGE:  {j['lang_note']}\n"
            if j.get('agency_note'):
                body += f"AGENCY:    {j['agency_note']}\n"
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
            body += f"JD BODY:   {j.get('jd_note', 'n/a')}\n"
            if j.get('lang_note'):
                body += f"LANGUAGE:  {j['lang_note']}\n"
            if j.get('agency_note'):
                body += f"AGENCY:    {j['agency_note']}\n"
            body += f"LINK:      {j['url']}\n"
            body += "-" * 40 + "\n\n"

    if board_matches:
        body += f"\n### SWISS BOARDS ({len(board_matches)})\n\n"
        for j in board_matches:
            body += f"ROLE:      {j['title']}\n"
            body += f"COMPANY:   {j.get('company') or '(see posting)'}\n"
            body += f"LOCATION:  {j.get('location') or '(see posting)'}\n"
            if j.get("posted"):
                body += f"POSTED:    {j['posted']}\n"
            body += f"JD BODY:   {j.get('jd_note', 'n/a')}\n"
            if j.get('lang_note'):
                body += f"LANGUAGE:  {j['lang_note']}\n"
            if j.get('agency_note'):
                body += f"AGENCY:    {j['agency_note']}\n"
            body += f"SOURCE:    {j.get('source', 'swiss-board')}\n"
            body += f"LINK:      {j['url']}\n"
            body += "-" * 40 + "\n\n"

    if not adzuna_matches and not swiss_matches and not board_matches:
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
    seen_adzuna = load_seen_adzuna()
    raw_jobs = search_adzuna()
    adzuna_matches = process_adzuna(raw_jobs, cooldowns, seen_adzuna)
    save_seen_adzuna(seen_adzuna)

    print("\n--- SWISS EMPLOYER DIRECT SCRAPE ---")
    try:
        swiss_raw = fetch_swiss_employer_jobs(state_path="seen_swiss_jobs.json")
        swiss_matches = filter_swiss_by_cooldown(swiss_raw, cooldowns)
    except Exception as e:
        print(f"[WARN] Swiss scrape failed: {e}")
        swiss_matches = []

    print("\n--- SWISS BOARD SCRAPE (Swissolar / JobScout24 / Fachplanung) ---")
    try:
        board_raw = fetch_swiss_board_jobs(state_path="seen_board_jobs.json")
        board_matches = filter_swiss_by_cooldown(board_raw, cooldowns)
    except Exception as e:
        print(f"[WARN] Swiss board scrape failed: {e}")
        board_matches = []

    expiring = format_expiring_soon(cooldowns, days=30)

    print(f"\n--- SUMMARY ---")
    print(f"Adzuna matches: {len(adzuna_matches)}")
    print(f"Swiss direct matches: {len(swiss_matches)}")
    print(f"Swiss board matches: {len(board_matches)}")
    print(f"Cooldowns expiring soon: {len(expiring)}")

    if adzuna_matches or swiss_matches or board_matches or expiring:
        send_email(adzuna_matches, swiss_matches, expiring, board_matches)
    else:
        print("No matches and no expiring cooldowns — no email sent.")


if __name__ == "__main__":
    main()
