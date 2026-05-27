# Integration Guide — Adzuna + Swiss Employer Direct, Combined Pipeline

## What you're replacing

Three files in your `pv-job-alert` repo:

```
pv-job-alert/
├── pv_job_alert.py        ← REPLACE with the new version
├── job_filters.py         ← NEW file (shared filter chain)
├── swiss_employers.py     ← NEW file (Swiss direct scrapers)
├── config.json            ← unchanged
├── seen_swiss_jobs.json   ← will be created on first run
└── .github/workflows/job_alert.yml   ← needs one small edit (see below)
```

## What changed in `pv_job_alert.py`

1. **Imports added** for `job_filters` and `swiss_employers`
2. **New `process_adzuna()` function** that wraps your existing scoring with the hard filter chain BEFORE scoring
3. **Existing `score_job()` retained unchanged** — your scoring logic still applies to jobs that pass the hard filter
4. **HARD_BLOCKERS list trimmed**: removed `personalverantwortung`, `teamleiter`, `führung von mitarbeitenden`, `disziplinarische führung` — these describe normal senior PM responsibilities and shouldn't be auto-blockers. Language and physical-work blockers retained.
5. **Email body restructured** into two sections: Adzuna aggregator results + Swiss employer direct results

## What the new filter chain does to Adzuna results

For each Adzuna job, BEFORE scoring:

1. **Reject if not in Switzerland** (Adzuna's `ch` endpoint leaks German cross-border jobs — this stops them)
2. **Fetch the full JD body** from `redirect_url` (Adzuna's short description is too truncated for language detection)
3. **Reject if function is wrong** (installer/technician/monteur/foreman titles without PM keyword)
4. **Reject if junior/intern/assistant**
5. **Reject if title contains wrong tech** (hydro/wind/HVAC/nuclear/etc.)
6. **Reject if JD is dominantly written in French or Italian**
7. **Reject if FR/IT stated as mandatory in JD** (without an `atout`/`asset`/`plus` softener nearby)

Only jobs surviving ALL filters get scored by your existing scoring engine, and only those scoring ≥50% get into the email.

## Why I trimmed your HARD_BLOCKERS list

`personalverantwortung` (personnel responsibility) and `teamleiter` (team lead) describe normal senior PM scope. Your CV already shows you coordinate stakeholders, vendors, and EPC teams. Auto-rejecting these would filter out roles where you should be senior enough to lead. If you want them back as blockers, add them to `HARD_BLOCKERS` in `pv_job_alert.py`.

## Workflow YAML edit

Your existing `.github/workflows/job_alert.yml` needs the state file persisted between runs. Add this step **after** the step that runs `python pv_job_alert.py`:

```yaml
      - name: Persist Swiss state file
        run: |
          if [ -f seen_swiss_jobs.json ]; then
            git config --global user.name "github-actions[bot]"
            git config --global user.email "github-actions[bot]@users.noreply.github.com"
            git add seen_swiss_jobs.json
            git diff --cached --quiet || git commit -m "chore: update Swiss seen-jobs state [skip ci]"
            git push
          fi
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

And ensure the workflow has write permission. Either add at the top of the workflow:

```yaml
permissions:
  contents: write
```

…or grant it once in Settings → Actions → General → "Read and write permissions."

## What to expect

- **Adzuna side**: previously most "Apply" verdicts were sketchy because Adzuna leaks German jobs and the short description can't catch French requirements. Expect 0-2 fewer Adzuna matches per week, but the ones that survive are real.
- **Swiss direct side**: 0-2 new matches per week from Axpo/Alpiq/Groupe-E/SUSI. Empty most weeks — that's accurate, not a bug.
- **Combined email**: single weekly digest with both sections clearly labeled.

## First-run noise

The Swiss employer scraper will treat ALL currently-posted jobs that match your filters as "new" on its first run. From today's data, that's 0 jobs — so no noise.

If the Swiss scraper later starts finding matches and you don't want a backlog dump, do this once manually before the next scheduled run:

```bash
python -c "from swiss_employers import fetch_swiss_employer_jobs; fetch_swiss_employer_jobs()"
git add seen_swiss_jobs.json && git commit -m "seed state" && git push
```

## When something breaks

Three failure modes are isolated:

1. **One Swiss scraper fails** (site layout changed) → only that employer drops out, others continue
2. **All Swiss scrapers fail** → Adzuna pipeline still runs and sends email
3. **Adzuna API down** → Swiss scraper still runs and sends email

Symptom diagnosis:
- Empty email for several weeks → check Adzuna app ID quota, then run `python swiss_employers.py` locally to see what fails
- Suspicious-looking jobs leaking through → check `job_filters.py` logs for the filter reasoning; tighten lists if needed

## Things this does NOT do

- Does NOT cover BKW, MET, Smartenergy, BW ESS, 49Komma8 (JS-rendered sites — would need browser automation)
- Does NOT cover LinkedIn (use Apify actor manually when needed)
- Does NOT cover jobs.ch / Indeed (Adzuna already pulls from those when employers syndicate; direct scraping gets rate-limited fast)
- Does NOT detect German level (B1 vs B2 vs C1) — your filter rules said keep ambiguous German-required jobs and read JD yourself
