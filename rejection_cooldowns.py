"""
rejection_cooldowns.py — Time-based rejection blocklist manager.

Companion to the hardcoded REJECTED_COMPANIES list in pv_job_alert.py.
Hardcoded list = permanent / manual blocks. This module = time-based cooldowns
from explicit rejections and auto-ATS rejections.

Public functions:
  load_cooldowns(path) -> list[dict]
      Load + auto-prune expired entries, write back cleaned list.

  is_blocked(company, title, cooldowns) -> tuple[bool, dict | None]
      True if job matches an active cooldown. Returns the matching entry.

  format_expiring_soon(cooldowns, days=30) -> list[dict]
      Entries whose cooldown expires within N days — for email warning section.

JSON schema (one entry per rejection event):
  {
    "company": str,                  # lowercase substring matched against company name
    "rejection_date": "YYYY-MM-DD",
    "blocked_until": "YYYY-MM-DD",
    "rejection_type": "explicit" | "auto-ats",
    "block_scope": "company" | "role",
    "role_pattern": str | null,      # regex (case-insensitive) when block_scope=role
    "source": str                    # provenance: "manual-YYYY-MM-DD" or "gmail-scrape"
  }
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime, timedelta

log = logging.getLogger(__name__)


def _today() -> date:
    return date.today()


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def load_cooldowns(path: str = "rejection_cooldowns.json") -> list[dict]:
    """Load cooldowns and prune expired entries. Writes cleaned list back to disk
    if any entries were removed.
    """
    if not os.path.exists(path):
        log.info("No cooldown file at %s — starting fresh", path)
        return []

    try:
        with open(path, "r", encoding="utf-8") as f:
            entries = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Could not read cooldown file %s (%s) — treating as empty", path, e)
        return []

    today = _today()
    active: list[dict] = []
    expired: list[dict] = []
    for e in entries:
        try:
            blocked_until = _parse_date(e["blocked_until"])
        except (KeyError, ValueError):
            log.warning("Skipping malformed cooldown entry: %s", e)
            continue
        if blocked_until >= today:
            active.append(e)
        else:
            expired.append(e)

    if expired:
        log.info("Pruning %d expired cooldown(s): %s",
                 len(expired), [e["company"] for e in expired])
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(active, f, indent=2, ensure_ascii=False)
        except OSError as e:
            log.error("Could not write pruned cooldown file: %s", e)

    return active


def is_blocked(company: str, title: str, cooldowns: list[dict]) -> tuple[bool, dict | None]:
    """Check if a job is in active cooldown.

    Match logic:
      - company-scope block: company name substring match (case-insensitive)
      - role-scope block: company name AND role_pattern regex must both match
    """
    company_lower = (company or "").lower()
    title_lower = (title or "").lower()

    for entry in cooldowns:
        entry_company = entry.get("company", "").lower()
        if not entry_company or entry_company not in company_lower:
            continue

        scope = entry.get("block_scope", "company")
        if scope == "company":
            return True, entry

        # role-scope: must also match the title pattern
        pattern = entry.get("role_pattern")
        if pattern and re.search(pattern, title_lower, flags=re.IGNORECASE):
            return True, entry

    return False, None


def format_expiring_soon(cooldowns: list[dict], days: int = 30) -> list[dict]:
    """Return entries whose cooldown expires within `days` days."""
    today = _today()
    threshold = today + timedelta(days=days)
    out = []
    for entry in cooldowns:
        try:
            blocked_until = _parse_date(entry["blocked_until"])
        except (KeyError, ValueError):
            continue
        if today <= blocked_until <= threshold:
            out.append({
                **entry,
                "days_remaining": (blocked_until - today).days,
            })
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    cd = load_cooldowns("rejection_cooldowns.json")
    print(f"\n{len(cd)} active cooldown(s):\n")
    for e in cd:
        print(f"  {e['company']:25s} until {e['blocked_until']}  [{e['block_scope']}/{e['rejection_type']}]")
        if e.get("role_pattern"):
            print(f"    role pattern: {e['role_pattern']}")

    print("\n--- Expiring within 30 days ---")
    soon = format_expiring_soon(cd)
    if soon:
        for e in soon:
            print(f"  {e['company']:25s} in {e['days_remaining']} days ({e['blocked_until']})")
    else:
        print("  (none)")
