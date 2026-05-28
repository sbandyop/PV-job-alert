"""
swiss_employers.py — Direct careers-page scrapers for Swiss IPP/utility employers.
Uses job_filters.py for the shared filter chain.
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

from job_filters import (
    apply_filter_chain,
    fetch_jd_body,
    is_swiss_location,
)

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (compatible; pv-job-alert/1.0)"
TIMEOUT = 15


def _fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Language": "en;q=0.9, de;q=0.8, fr;q=0.7",
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", errors="replace")


def _strip_tags(s: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", s))).strip()


# ============================================================================
# Per-employer list scrapers
# ============================================================================

def scrape_axpo() -> list[dict]:
    jobs: list[dict] = []
    seen_urls: set[str] = set()
    for page in range(1, 25):
        html = _fetch(f"https://careers.axpo.com/jobs?page={page}")
        pattern = re.compile(
            r'<a[^>]+href="(https://careers\.axpo\.com/jobs/\d+-[^"]+)"[^>]*>'
            r'(.*?)</a>(.*?)'
            r'(?=<a[^>]+href="https://careers\.axpo\.com/jobs|</main|</body)',
            re.S,
        )
        page_before = len(jobs)
        for url, inner, after in pattern.findall(html):
            if url in seen_urls:
                continue
            seen_urls.add(url)
            title = _strip_tags(inner)
            spans = re.findall(r'<span[^>]*>([^<&][^<]*)</span>', after[:800])
            real_spans = [s.strip() for s in spans if s.strip() and len(s.strip()) > 1]
            location = real_spans[1] if len(real_spans) >= 2 else ""
            workmode = real_spans[2] if len(real_spans) >= 3 else ""
            jobs.append({
                "company": "Axpo", "title": title, "url": url,
                "location": location, "workmode": workmode,
            })
        if len(jobs) == page_before:
            break
    return jobs


def scrape_alpiq() -> list[dict]:
    jobs: list[dict] = []
    seen_urls: set[str] = set()
    for startrow in range(0, 500, 25):
        html = _fetch(
            f"https://jobs.alpiq.com/search/?q=&locationsearch=&startrow={startrow}"
        )
        page_before = len(jobs)
        for href, title in re.findall(
            r'<a[^>]+href="(/job/[^"]+/\d+/)"[^>]*>([^<]+)</a>',
            html,
        ):
            url = "https://jobs.alpiq.com" + href
            if url in seen_urls:
                continue
            seen_urls.add(url)
            parts = href.split("/")
            slug = parts[2] if len(parts) > 2 else ""
            city_token = slug.split("-")[0] if slug else ""
            location = urllib.parse.unquote(city_token)
            jobs.append({
                "company": "Alpiq", "title": unescape(title).strip(), "url": url,
                "location": location, "workmode": "",
            })
        if len(jobs) == page_before:
            break
    return jobs


def scrape_groupe_e() -> list[dict]:
    jobs: list[dict] = []
    seen_urls: set[str] = set()
    for startrow in range(0, 500, 25):
        html = _fetch(
            f"https://job.groupe-e.ch/search/?q=&locationsearch=&startrow={startrow}"
        )
        page_before = len(jobs)
        for href, title in re.findall(
            r'<a[^>]+href="(/job/[^"]+/\d+/)"[^>]*>([^<]+)</a>',
            html,
        ):
            url = "https://job.groupe-e.ch" + href
            if url in seen_urls:
                continue
            seen_urls.add(url)
            parts = href.split("/")
            slug = parts[2] if len(parts) > 2 else ""
            city_token = slug.split("-")[0] if slug else ""
            location = urllib.parse.unquote(city_token)
            jobs.append({
                "company": "Groupe-E", "title": unescape(title).strip(), "url": url,
                "location": location, "workmode": "",
            })
        if len(jobs) == page_before:
            break
    return jobs


def scrape_susi() -> list[dict]:
    html = _fetch("https://www.susi-partners.com/career/")
    out: list[dict] = []
    link_pattern = re.compile(r'href="(https://[^"]+ApplyJob\?vacancyNo=VN(\d+))"')
    matches = list(link_pattern.finditer(html))
    for i, m in enumerate(matches):
        url, vn = m.group(1), m.group(2)
        window_end = matches[i + 1].start() if i + 1 < len(matches) else m.end() + 500
        snippet = html[m.end():window_end]
        text_chunk = re.split(r'<a\b', snippet, maxsplit=1)[0]
        full_text = _strip_tags(text_chunk)
        full_text = re.sub(r"\b(View Job|Apply|Bewerben|Postuler)\b.*$", "", full_text, flags=re.I).strip()
        parts = full_text.rsplit(maxsplit=1)
        if len(parts) == 2 and parts[1] in {"Zurich", "Zürich", "Singapore", "Frankfurt", "London"}:
            title, location = parts
        else:
            title, location = full_text or f"SUSI Vacancy VN{vn}", ""
        out.append({
            "company": "SUSI Partners", "title": title.strip(), "url": url,
            "location": location, "workmode": "",
        })
    return out


def scrape_edisun() -> list[dict]:
    """Edisun Power — static HTML careers page."""
    try:
        html = _fetch("https://www.edisunpower.com/de/karriere")
    except Exception:
        try:
            html = _fetch("https://www.edisunpower.com/en/career")
        except Exception:
            return []
    out: list[dict] = []
    for href, title in re.findall(
        r'<a[^>]+href="([^"]*(?:karriere|stelle|job|career)[^"]*)"[^>]*>([^<]{5,})</a>',
        html, re.I
    ):
        title = _strip_tags(title).strip()
        if not title or len(title) < 4:
            continue
        url = href if href.startswith("http") else "https://www.edisunpower.com" + href
        out.append({
            "company": "Edisun Power", "title": title, "url": url,
            "location": "Zürich", "workmode": "",
        })
    return out


SCRAPERS: list[tuple[str, Callable[[], list[dict]]]] = [
    ("Axpo", scrape_axpo),
    ("Alpiq", scrape_alpiq),
    ("Groupe-E", scrape_groupe_e),
    ("SUSI Partners", scrape_susi),
    ("Edisun Power", scrape_edisun),
]


# ============================================================================
# Dedupe + state
# ============================================================================

def _jd_fingerprint(jd_body: str) -> str:
    if not jd_body:
        return ""
    return re.sub(r"\s+", " ", jd_body.lower()).strip()[:500]


def _dedupe_by_jd(jobs: list[dict]) -> list[dict]:
    seen_fps: set[str] = set()
    out: list[dict] = []
    for j in jobs:
        fp = _jd_fingerprint(j.get("jd_body", ""))
        if fp and fp in seen_fps:
            log.info("  DEDUPE: '%s' (duplicate JD)", j["title"])
            continue
        if fp:
            seen_fps.add(fp)
        out.append(j)
    return out


def _load_state(path: str) -> set[str]:
    if not os.path.exists(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Could not read state file %s (%s)", path, e)
        return set()


def _save_state(path: str, urls: set[str]) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(sorted(urls), f, indent=2, ensure_ascii=False)
    except OSError as e:
        log.error("Could not write state file %s: %s", path, e)


# ============================================================================
# Public entry
# ============================================================================

def fetch_swiss_employer_jobs(state_path: str = "seen_swiss_jobs.json") -> list[dict]:
    """Run all scrapers, apply shared filter chain, return new matching jobs."""
    seen_before = _load_state(state_path)
    all_jobs: list[dict] = []
    all_current_urls: set[str] = set()

    for name, fn in SCRAPERS:
        try:
            jobs = fn()
            log.info("[%s] scraped %d jobs", name, len(jobs))
            all_jobs.extend(jobs)
            all_current_urls.update(j["url"] for j in jobs)
        except urllib.error.HTTPError as e:
            log.error("[%s] HTTP %s", name, e.code)
        except urllib.error.URLError as e:
            log.error("[%s] network error: %s", name, e.reason)
        except Exception as e:
            log.error("[%s] failed: %s", name, e)

    # Pre-filter on title/location (no JD fetch needed)
    candidates = []
    for j in all_jobs:
        if j["url"] in seen_before:
            continue
        if not is_swiss_location(j.get("location", "")):
            continue
        candidates.append(j)

    # Fetch JD for survivors, then apply full filter chain
    survivors = []
    for j in candidates:
        try:
            jd_body = fetch_jd_body(j["url"])
        except Exception as e:
            log.warning("  JD fetch failed for %s: %s", j["title"], e)
            jd_body = ""
        j["jd_body"] = jd_body

        keep, reason = apply_filter_chain(
            title=j["title"],
            location=j.get("location", ""),
            jd_body=jd_body,
            workmode=j.get("workmode", ""),
            require_pm_keyword=True,  # unfiltered employer scrape — need PM gate
        )
        if not keep:
            log.info("  REJECT %s: %s", j["title"], reason)
            continue
        survivors.append(j)

    final = _dedupe_by_jd(survivors)

    for j in final:
        j.pop("jd_body", None)
        j["source"] = "swiss-direct"

    _save_state(state_path, seen_before | all_current_urls)

    log.info("Pipeline: %d scraped -> %d location-pass -> %d filter-pass -> %d final",
             len(all_jobs), len(candidates), len(survivors), len(final))
    return final


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    test_state = "/tmp/test_swiss_state.json"
    if os.path.exists(test_state):
        os.remove(test_state)
    new_jobs = fetch_swiss_employer_jobs(state_path=test_state)
    print(f"\n=== {len(new_jobs)} new matching jobs ===\n")
    for j in new_jobs:
        loc = f" [{j['location']}]" if j["location"] else ""
        print(f"  [{j['company']}]{loc} {j['title']}")
        print(f"    {j['url']}\n")

