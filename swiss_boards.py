"""
swiss_boards.py - Swiss sector job boards and Fachplanung careers pages.

Companion to swiss_employers.py. Same contract: scrape list pages, apply the shared
filter chain from job_filters.py, return only new matching jobs.

Sources verified reachable from a datacenter IP on 2026-08-06:
  - Swissolar Stellenboerse    (HTTP 200, plain fetch, ~318 KB, stable label markup)
  - JobScout24                 (HTTP 200, plain fetch)
  - Fachplanung careers pages  (per-employer, static HTML)

Verified 2026-08-15:
  - job-room.ch / arbeit.swiss (HTTP 200, public JSON API, no auth, full JD in payload)

Deliberately NOT here:
  - energie-job.ch, jobagent.ch - DataDome bot wall, HTTP 403 from any datacenter IP
    regardless of User-Agent. Cannot be scraped from GitHub Actions. Covered instead by
    their own email Job-Abo and by Chrome in the daily run.
  - x28.ch - labour-market data provider, not a job board. No public job index.
  - jobs.ch - cookie-consent gate blocks the body server-side. Chrome-only.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from html import unescape
from typing import Callable

from job_filters import (agency_flag, apply_filter_chain, fetch_jd_body,
                         is_swiss_location, german_prescreen_flag)

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (compatible; pv-job-alert/1.0)"
TIMEOUT = 20


def _fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Language": "de-CH,de;q=0.9,fr;q=0.7,en;q=0.6",
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", errors="replace")


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


# ============================================================================
# Swissolar Stellenboerse
# ============================================================================

SWISSOLAR_URL = "https://www.swissolar.ch/de/angebot/stellenboerse"
_DATE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")
_STOP_PREFIXES = ("Autres offres", "Ulteriori offerte", "Haben Sie Fragen")


def scrape_swissolar() -> list[dict]:
    """Swissolar member job board.

    Flattening tags yields a repeating labelled sequence per advert:
        <title> Firma <company> Ort <town> Weitere Angaben Stellenbeschreibung
        Publikation <DD.MM.YYYY>
    Anchored on 'Aktuelle Stellenangebote' to skip the Stellensuchende block above it.
    Verified against 20 live adverts on 2026-08-06.
    """
    try:
        html = _fetch(SWISSOLAR_URL)
    except Exception as e:
        log.error("[Swissolar] fetch failed: %s", e)
        return []

    lines = [l.strip() for l in unescape(re.sub(r"<[^>]+>", "\n", html)).split("\n") if l.strip()]

    anchors = [i for i, l in enumerate(lines) if "Aktuelle Stellenangebote" in l]
    if not anchors:
        log.error("[Swissolar] anchor 'Aktuelle Stellenangebote' missing - markup changed")
        return []

    out: list[dict] = []
    i = anchors[-1] + 1
    while i < len(lines):
        title = lines[i]
        if title.startswith(_STOP_PREFIXES):
            break
        if i + 2 >= len(lines) or lines[i + 1] != "Firma":
            i += 1
            continue
        company = lines[i + 2]
        town = lines[i + 4] if (i + 4 < len(lines) and lines[i + 3] == "Ort") else ""
        pub = ""
        for j in range(i + 5, min(i + 12, len(lines))):
            if _DATE_RE.match(lines[j]):
                pub = lines[j]
                break
        out.append({
            "company": company,
            "title": title,
            # No per-advert permalink exists - adverts are PDFs or employer links behind a
            # 'Stellenbeschreibung' anchor. Synthesise a stable key for state and dedupe.
            "url": SWISSOLAR_URL + "#" + urllib.parse.quote(company + "|" + title),
            "location": town,
            "workmode": "",
            "posted": pub,
            "board": "Swissolar",
        })
        i += 5

    log.info("[Swissolar] parsed %d adverts", len(out))
    return out


# ============================================================================
# JobScout24
# ============================================================================

JOBSCOUT24_QUERIES = [
    "projektleiter photovoltaik",
    "projektleiter batteriespeicher",
    "bauherrenvertreter",
    "due diligence energie",
]


def scrape_jobscout24() -> list[dict]:
    """JobScout24 keyword search. Static HTML list; one request per query."""
    out: list[dict] = []
    seen: set[str] = set()
    for q in JOBSCOUT24_QUERIES:
        url = "https://www.jobscout24.ch/de/jobs/" + urllib.parse.quote(q) + "/"
        try:
            html = _fetch(url)
        except Exception as e:
            log.warning("[JobScout24] '%s' failed: %s", q, e)
            continue
        for href, block in re.findall(
            r'<a[^>]+href="(/de/job/[^"]+)"[^>]*>(.{0,400}?)</a>', html, re.I | re.S
        ):
            title = _text(block)
            if not title or len(title) < 6:
                continue
            full = "https://www.jobscout24.ch" + href
            if full in seen:
                continue
            seen.add(full)
            out.append({
                "company": "",   # resolved from the JD body downstream
                "title": title,
                "url": full,
                "location": "",  # JobScout24 keeps the town on the detail page
                "workmode": "",
                "board": "JobScout24",
            })
    log.info("[JobScout24] parsed %d rows across %d queries", len(out), len(JOBSCOUT24_QUERIES))
    return out


# ============================================================================
# job-room.ch / arbeit.swiss - Swiss public employment service (SECO / RAV)
# ============================================================================
#
# Public JSON API, no auth, verified HTTP 200 from a datacenter IP on 2026-08-15.
# Results carry the full job description, so no per-job JD fetch is needed -
# unlike every other board here. Note /api/jobadvertisements/ returns 401; the
# working path is /jobadservice/api/jobAdvertisements/_search.
#
# Two source-specific hazards, both observed on 2026-08-14:
#   1. Keyword match is FULL TEXT, so a roofing or heating advert surfaces merely
#      because the employer's boilerplate paragraph mentions Solaranlagen. The
#      shared filter chain must see the description, which it does.
#   2. The corpus is dominated by Monteur / Installateur / Lernende listings and
#      by staffing agencies reselling one mandate under several names. A 25-row
#      sample contained zero professional owner-side roles. Expect low yield.

JOBROOM_API = ("https://www.job-room.ch/jobadservice/api/jobAdvertisements/_search"
               "?page=0&size={size}&sort=date_desc")
# 2026-08-15: three queries returned 86 rows and zero owner-side PV mandates.
# "Netzanschluss" pulls grid-electrician and utility roles (ewb Auftragsleiter
# Netzanschluesse, ewl Netzelektriker) and even telecom (Saphir Group Networks).
# "Bauherrenvertretung" pulls Hochbau owner-representation with no PV content -
# Drees & Sommer, Fuhr Buser, every cantonal Hochbauamt. Both terms describe the
# target profile; the intersection with PV is empty here because job-room matches
# FULL TEXT including employer boilerplate.
JOBROOM_QUERIES = ["Photovoltaik"]
JOBROOM_SIZE = 40
JOBROOM_WORKLOAD_MIN = 80


def _jobroom_post(query: str, size: int) -> list[dict]:
    body = json.dumps({"permanent": True, "keywords": [query]}).encode("utf-8")
    req = urllib.request.Request(
        JOBROOM_API.format(size=size),
        data=body,
        headers={
            "User-Agent": UA,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Language": "de-CH,de;q=0.9,fr;q=0.7,en;q=0.6",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def scrape_jobroom() -> list[dict]:
    """arbeit.swiss / job-room.ch public API. One POST per keyword."""
    out: list[dict] = []
    seen: set[str] = set()
    for q in JOBROOM_QUERIES:
        try:
            records = _jobroom_post(q, JOBROOM_SIZE)
        except Exception as e:
            log.warning("[JobRoom] '%s' failed: %s", q, e)
            continue
        for rec in records:
            ad = rec.get("jobAdvertisement") or {}
            content = ad.get("jobContent") or {}
            descs = content.get("jobDescriptions") or []
            if not descs or not ad.get("id"):
                continue
            title = _text(descs[0].get("title") or "")
            if not title:
                continue
            url = "https://www.job-room.ch/job-search/" + ad["id"]
            if url in seen:
                continue
            seen.add(url)

            empl = content.get("employment") or {}
            try:
                wl_max = int(empl.get("workloadPercentageMax") or 100)
            except (TypeError, ValueError):
                wl_max = 100
            if wl_max < JOBROOM_WORKLOAD_MIN:
                continue

            loc = content.get("location") or {}
            city = loc.get("city") or ""
            canton = loc.get("cantonCode") or ""

            out.append({
                "company": ((content.get("company") or {}).get("name") or "").strip(),
                "title": title,
                "url": url,
                "location": ", ".join(p for p in (city, canton) if p),
                "workmode": "",
                "board": "JobRoom",
                "_jd_inline": _text(descs[0].get("description") or ""),
            })
    log.info("[JobRoom] parsed %d rows across %d queries", len(out), len(JOBROOM_QUERIES))
    return out


# ============================================================================
# Fachplanung / small independent offices
# ============================================================================

FACHPLANUNG_PAGES: list[tuple[str, str]] = [
    ("Energie Netzwerk",   "https://www.energie-netzwerk.ch/jobs"),
    ("Evergy",             "https://www.evergy.ch/karriere"),
    ("Basler & Hofmann",   "https://www.baslerhofmann.ch/de/karriere/offene-stellen.html"),
    ("Amstein + Walthert", "https://amstein-walthert.ch/de/karriere/"),
    ("eicher+pauli",       "https://www.eicher-pauli.ch/jobs"),
    ("Gruner",             "https://www.gruner.ch/de/karriere"),
    ("Enerpeak",           "https://www.enerpeak.ch/jobs"),
    ("Planeco",            "https://www.planeco.ch/jobs"),
    ("BE Netz",            "https://www.benetz.ch/ueber-uns/jobs"),
    ("Edisun Power",       "https://www.edisunpower.com/de/karriere"),
]

_JOB_HREF = re.compile(
    r'<a[^>]+href="([^"]*(?:job|stelle|karriere|career|vacanc)[^"]*)"[^>]*>([^<]{6,120})</a>',
    re.I,
)
_GENERIC_TITLES = {"jobs", "karriere", "career", "offene stellen", "stellen", "vacancies"}


def scrape_fachplanung() -> list[dict]:
    out: list[dict] = []
    for company, page in FACHPLANUNG_PAGES:
        try:
            html = _fetch(page)
        except Exception as e:
            log.info("[Fachplanung] %s unreachable (%s) - skipped", company, e)
            continue
        parts = urllib.parse.urlsplit(page)
        base = parts.scheme + "://" + parts.netloc
        seen_here: set[str] = set()
        for href, title in _JOB_HREF.findall(html):
            title = _text(title)
            if not title or title.lower() in _GENERIC_TITLES:
                continue
            if href.startswith("http"):
                url = href
            elif href.startswith("/"):
                url = base + href
            else:
                url = base + "/" + href
            if url in seen_here:
                continue
            seen_here.add(url)
            out.append({
                "company": company, "title": title, "url": url,
                "location": "", "workmode": "", "board": "Fachplanung",
            })
    log.info("[Fachplanung] parsed %d rows across %d employers", len(out), len(FACHPLANUNG_PAGES))
    return out


# ============================================================================
# Dedupe + state
# ============================================================================

SCRAPERS: list[tuple[str, Callable[[], list[dict]]]] = [
    ("Swissolar", scrape_swissolar),
    ("JobScout24", scrape_jobscout24),
    ("Fachplanung", scrape_fachplanung),
    ("JobRoom", scrape_jobroom),
]


def _load_state(path: str) -> set[str]:
    if not os.path.exists(path):
        return set()
    try:
        with open(path, encoding="utf-8") as f:
            return set(json.load(f).get("seen_urls", []))
    except Exception:
        return set()


def _save_state(path: str, urls: set[str]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"seen_urls": sorted(urls)}, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def fetch_swiss_board_jobs(state_path: str = "seen_board_jobs.json") -> list[dict]:
    """Run all board scrapers, apply the shared filter chain, return new matches."""
    seen_before = _load_state(state_path)
    all_jobs: list[dict] = []
    all_urls: set[str] = set()

    for name, fn in SCRAPERS:
        try:
            jobs = fn()
            all_jobs.extend(jobs)
            all_urls.update(j["url"] for j in jobs)
        except urllib.error.HTTPError as e:
            log.error("[%s] HTTP %s", name, e.code)
        except urllib.error.URLError as e:
            log.error("[%s] network error: %s", name, e.reason)
        except Exception as e:
            log.error("[%s] failed: %s", name, e)

    # Location is often blank on board list pages. Only reject on a POSITIVE non-CH
    # signal, never on absence - the JD-stage filter catches the rest.
    candidates = [
        j for j in all_jobs
        if j["url"] not in seen_before
        and (not j.get("location") or is_swiss_location(j["location"]))
    ]

    survivors: list[dict] = []
    for j in candidates:
        jd_body = j.pop("_jd_inline", "")
        if not jd_body:
            try:
                jd_body = fetch_jd_body(j["url"])
            except Exception as e:
                log.warning("  JD fetch failed for %s: %s", j["title"], e)
                jd_body = ""
        if not jd_body:
            log.warning("  EMPTY BODY: %s | %s", j["title"], j["url"])
        j["jd_note"] = (f"{len(jd_body)} chars" if jd_body
                        else "NOT RETRIEVED - requirements unverified")
        j["lang_note"] = german_prescreen_flag(jd_body)
        j["agency_note"] = agency_flag(j.get("company", ""), jd_body)
        keep, reason = apply_filter_chain(
            title=j["title"],
            location=j.get("location", ""),
            jd_body=jd_body,
            workmode=j.get("workmode", ""),
            company=j.get("company", ""),
            require_pm_keyword=True,
            require_body=True,
        )
        if not keep:
            log.info("  REJECT %s: %s", j["title"], reason)
            continue
        j["jd_body"] = jd_body  # for the domain-relevance gate downstream
        j["source"] = "swiss-board:" + j.get("board", "?")
        survivors.append(j)

    _save_state(state_path, seen_before | all_urls)
    log.info("[swiss_boards] %d new matching jobs", len(survivors))
    return survivors
