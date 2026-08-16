"""
LinkedIn source via Apify — Soumi Bandyopadhyay

Added 2026-08-16. RATIONALE: the Apify actor used to be invoked from Claude's
MCP connector, which put an interactive dependency in the middle of an
unattended scheduled job. On 2026-08-15 that connector dropped and the whole
LinkedIn sweep returned nothing (HTTP 404 from the MCP proxy, twice) while the
actor itself was demonstrably healthy — 391,130 runs in the preceding 30 days.
Apify exposes a plain REST endpoint, so the workflow calls it directly and the
results arrive in the same digest email as every other source. Nothing has to
be connected by hand before a run.

Apify scrapes on its own proxy infrastructure, so the GitHub runner's IP is
irrelevant here. That is why LinkedIn works from Actions while energie-job.ch
and jobagent.ch (DataDome, HTTP 403 to any datacenter IP) still cannot.

COST: PAY_PER_EVENT. USD 0.002 per result on the Apify free plan, USD 0.001 on
Bronze and above, plus USD 0.00005 per actor start. Pricing changed 2026-08-14;
the older "USD 2 per 1000 results" figure is now correct only for paid plans.
MAX_ITEMS caps each run at 25 results, so roughly USD 0.05 per run.
"""

import os
import json
import requests
from urllib.parse import urlsplit, urlunsplit

from job_filters import (agency_flag, apply_filter_chain, german_prescreen_flag)

# Optional on purpose: os.environ.get, not os.environ[...]. If the secret is
# missing the LinkedIn source is skipped and the rest of the digest still goes
# out. A missing token must never take the whole run down.
APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "")

ACTOR_ID = "curious_coder~linkedin-jobs-scraper"
MAX_ITEMS = 25
TIMEOUT_SECS = 300          # run-sync-get-dataset-items hard-fails past 300s

# Do NOT add a role title to `keywords`. Verified 2026-08-14: "Photovoltaik"
# alone returned 10/10 on-domain results, whereas "Projektleiter Photovoltaik"
# returned 2/50 — LinkedIn's AI search ranks on the highest-frequency token and
# pads the item cap with unrelated project-management roles.
#
# Every field is stated explicitly. Omitted fields silently inherit the Actor's
# own defaults, which have changed under us before.
ACTOR_INPUT = {
    "urls": [],
    "keywords": "Photovoltaik",
    "location": "Switzerland",
    "datePosted": "pastWeek",
    "limitPerSource": 25,
    "scrapeCompany": True,          # free at this price tier; see headcount note
    "autoConvertToAiSearch": True,
    "under10Applicants": False,
    "companyIds": [],
    "splitByLocation": False,
}


def _canonical(url):
    """Strip the query string from a LinkedIn job URL.

    refId, trackingId, position and pageNum change on every single scrape, so
    dedupe on the raw URL never matches and the same advert reappears weekly.
    """
    if not url:
        return ""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def _load_seen(path):
    try:
        with open(path) as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def _save_seen(seen, path):
    with open(path, "w") as f:
        json.dump(sorted(seen), f, indent=2)


def _company_of(item):
    company = item.get("companyName") or ""
    if not company and isinstance(item.get("company"), dict):
        company = item["company"].get("name", "")
    return company


def fetch_linkedin_jobs(state_path="seen_linkedin_jobs.json"):
    """Return new LinkedIn jobs, shaped like the swiss_boards / swiss_employers
    payloads so filter_swiss_by_cooldown and send_email can consume them
    unchanged."""
    if not APIFY_TOKEN:
        print("  APIFY_TOKEN not set — LinkedIn source skipped (not an error)")
        return []

    endpoint = (f"https://api.apify.com/v2/acts/{ACTOR_ID}"
                f"/run-sync-get-dataset-items?token={APIFY_TOKEN}"
                f"&maxItems={MAX_ITEMS}")
    try:
        resp = requests.post(endpoint, json=ACTOR_INPUT, timeout=TIMEOUT_SECS)
        resp.raise_for_status()
        items = resp.json()
    except Exception as e:
        print(f"  [WARN] Apify LinkedIn fetch failed: {e}")
        return []

    if not isinstance(items, list):
        print(f"  [WARN] Apify returned {type(items).__name__}, expected list")
        return []

    seen = _load_seen(state_path)
    jobs = []

    for it in items:
        link = it.get("link") or it.get("jobUrl") or it.get("url") or ""
        key = _canonical(link) or str(it.get("id", ""))
        if not key or key in seen:
            continue

        title = it.get("title", "")
        company = _company_of(it)
        location = it.get("location", "")
        desc = it.get("descriptionText") or it.get("description") or ""

        keep, reason = apply_filter_chain(
            title=title, location=location, jd_body=desc,
            workmode=it.get("workplaceType", "") or "",
            short_description=desc, company=company,
        )
        if not keep:
            print(f"  LINKEDIN FILTER ({reason}): {title}")
            seen.add(key)       # a structural reject will not become eligible later
            continue

        seen.add(key)
        jobs.append({
            "source": "linkedin-apify",
            "title": title,
            "company": company,
            "location": location,
            "url": link,
            "posted": it.get("postedAt") or it.get("publishedAt") or "",
            "jd_body": desc,
            "jd_note": (f"{len(desc)} chars from linkedin.com" if desc
                        else "NOT RETRIEVED - requirements unverified"),
            "lang_note": german_prescreen_flag(desc),
            "agency_note": agency_flag(company, desc),
            # companyEmployeesCount is the one field that separates a 15-person
            # installer from a utility or a fund, and it drives the segment-scale
            # check. The `industries` field is NOT used: it is unreliable, a
            # solar company has come back tagged "Oil and Gas".
            "headcount": it.get("companyEmployeesCount"),
        })

    _save_seen(seen, state_path)
    print(f"Retrieved {len(jobs)} new job(s) from LinkedIn via Apify")
    return jobs
