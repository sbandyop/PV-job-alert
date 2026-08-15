# Repo doc changes — 15.08.2026

Two changes. Both propose-only; commit them yourself via github.com.

---

## 1. Replace README.md

Current content is two lines and one of them is wrong:

```
# PV-job-alert
Weekly PV job search
```

It is not weekly — two crons, Tuesday and Wednesday.

Replace the whole file with `README.md` from this run.

**Method:** open `README.md` in the repo → pencil icon → select all → paste →
commit. Markdown has no auto-indent, so pasting into the web editor is safe here
(unlike Python).

---

## 2. Delete INTEGRATION.md

**Why it goes rather than gets rewritten.** It is a one-time migration guide —
"Three files in your repo ← REPLACE with the new version" — for a migration that
completed months ago. Its file inventory lists `pv_job_alert.py`, `job_filters.py`
and `swiss_employers.py` and predates `swiss_boards.py` entirely, so it now
describes a repo that does not exist. Everything in it that is still true has been
folded into the new README; everything else is actively misleading.

**Method:** open `INTEGRATION.md` → ⋯ menu → Delete file → commit.

Git history keeps it if you ever want it back.

---

## Not changed, and why

`config.json` — correct as-is, with usage instructions inline.

`requirements.txt` — `requests==2.31.0` is genuinely used by `pv_job_alert.py`
(the three scraper modules use `urllib` from the standard library). The pin is
2023-era but working. Bump only if a runner starts warning.

`seen_adzuna_jobs.json` (258 B), `seen_swiss_jobs.json` (32 KB),
`seen_board_jobs.json` (7 KB) — all healthy, all committed by the workflow since
the `git add` fix. Leave them alone.

`rejection_cooldowns.py` — live, imported by `pv_job_alert.py`. Not dead code.

`job_filters.py` (814 lines) — the shared spine. Every scraper imports it.

---

## Still outstanding from earlier in this session

`swiss_boards.py` — the job-room.ch port. File built, compiles, returns 86 live
rows across three queries. Not yet committed. Upload the staged `swiss_boards.py`
via Add file → Upload files (same filename replaces the existing one).

Commit that one **before** the next Tuesday cron if you want job-room.ch in this
week's digest.
