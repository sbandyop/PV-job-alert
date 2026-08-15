# PV-job-alert

Automated job search for utility-scale solar PV owner's-engineer roles in Switzerland
and the DACH region. Runs on GitHub Actions, emails a digest, and keeps its own
dedup and rejection-cooldown state.

## Schedule

Two crons per week, both defined in `.github/workflows/weekly_job_alert.yml`:

| Cron | UTC | Swiss time | Purpose |
|---|---|---|---|
| `0 10 * * 2` | Tue 10:00 | Tue ~12:00 | Catches the Tuesday posting peak |
| `0 7 * * 3` | Wed 07:00 | Wed ~09:00 | Sweeps late-Tuesday and overnight posts |

`workflow_dispatch` is enabled — run manually from the Actions tab any time.

To pause without disabling the workflow, set `paused` to `true` in `config.json`.

## Sources

| Module | Covers |
|---|---|
| `pv_job_alert.py` | Adzuna CH aggregator API; orchestrates everything else; sends the email |
| `swiss_employers.py` | 12 Swiss employers' own careers pages, scraped directly |
| `swiss_boards.py` | Swissolar Stellenbörse, JobScout24, job-room.ch (arbeit.swiss), and 10 Fachplanung careers pages |

**Deliberately not scraped**, and not worth retrying from a runner:

- `energie-job.ch`, `jobagent.ch` — DataDome bot wall, HTTP 403 from any datacenter
  IP regardless of User-Agent. Covered instead by their own email Job-Abo.
- `jobs.ch` — cookie-consent gate blocks the body server-side.
- `x28.ch` — labour-market data provider, not a job board. It *operates*
  energie-job.ch, jobagent.ch and jobscout24.ch and sends their alert mails from
  `info@x28.ch`; relevant for recognising senders, not for scraping.

## Filtering

All scrapers return raw rows and hand them to the shared chain in `job_filters.py`.
Nothing bypasses it. The chain rejects on structural grounds only — language wall,
geography, credential gates, trades or sales anchor, narrow-specialist core — never
on segment mismatch alone.

`rejection_cooldowns.py` blocks companies that have already rejected, for a period
recorded per entry in `rejection_cooldowns.json`.

## Files: state vs config

**Config — edit these by hand:**

- `config.json` — pause switch
- `requirements.txt` — Python deps (`requests`, used by `pv_job_alert.py` only;
  the scrapers use `urllib` from the standard library)

**State — written by the workflow, never edit by hand:**

- `seen_adzuna_jobs.json` — Adzuna IDs already reported
- `seen_swiss_jobs.json` — employer-direct URLs already reported
- `seen_board_jobs.json` — board URLs already reported
- `rejection_cooldowns.json` — company cooldowns; schema-validated, written by the
  daily assistant sync, not by the scraper

Hand-editing a `seen_*.json` file either re-reports everything already seen or
silently suppresses new jobs. If one needs resetting, delete it — the next run
recreates it — and expect that run's digest to be large.

The workflow commits all four state files back to `main` after each run. If a
source starts repeating the same jobs every run, its state file has stopped
persisting — check the `git add` line in the workflow first.

## Digest format

The email subject is prefixed `[PV Job Alert]`. The body is split into sections,
each with `ROLE:` / `COMPANY:` / `LOCATION:` / `FIT:` / `MATCH:` / `GAP:` / `LINK:`
fields:

```
### ADZUNA AGGREGATOR
### SWISS EMPLOYER DIRECT
### SWISS BOARDS
### COOLDOWN EXPIRING SOON
```

`FIT%` is keyword-derived and deliberately crude — it ranks, it does not decide.
Roles are re-scored downstream against the master CV.

## Adding a source

Add a `scrape_*()` returning a list of dicts with `company`, `title`, `url`,
`location`, `workmode` and `board`, then register it in that module's `SCRAPERS`
list. The aggregator applies the filter chain and state handling for you.

If the source ships the full job description in its listing response (as
job-room.ch does), attach it as `_jd_inline` and the aggregator will skip the
per-job JD fetch.

## Secrets

Set in repo Settings → Secrets and variables → Actions. Nothing is read from
anywhere else, and no token is ever committed.
