# wasl-listing-monitor

Checks [wasl.ae](https://www.wasl.ae/en/search/residential) on a schedule for **new
2-bedroom apartments in wasl Village with annual rent up to AED 60,000** and opens a
GitHub Issue assigned to the repository owner for each new match. GitHub emails the
assignee, so alerts arrive by email with no SMTP credentials or secrets of any kind.

Runs entirely in GitHub Actions; nothing needs to be switched on at home.

## How it works

1. `monitor.yml` runs on a cron schedule (and on demand). It fetches the search page
   (plain HTTP first, headless Chromium only if the page needs JavaScript), walks every
   result page, extracts each unit's reference (`IM001...`), rent, bedrooms, size and
   building, and re-applies the criteria in code. URL filters are never trusted.
2. Results are diffed against the committed ledger `state/seen.json`. New or re-listed
   units become an assigned issue labelled `wasl-alert`. Delivery is two-phase: a unit is
   stamped as alerted only after the issue exists, so a failed API call is retried next run.
3. Before any diff the run must prove the page is real: HTTP 200, no bot-challenge
   markers, the `1-12 of N records found` counter parsed, a broad control query returning
   a plausible total, and every page collected. Otherwise the run exits **DEGRADED**,
   touches no ledger, and after three consecutive failures opens a `MONITOR BROKEN` issue.
4. `watchdog.yml` runs daily on a separate code path and raises an issue if no successful
   run is recorded for several hours. A daily heartbeat comment on the `monitor status`
   issue proves the system is alive; its absence is itself the signal.

## Modes (Actions -> monitor -> Run workflow)

| mode | effect |
|---|---|
| `monitor` | full run, writes `state/`, opens issues |
| `dry-run` | fetch + parse + diff, prints what would be sent, writes nothing |
| `discovery` | probes robots.txt, sitemap, network calls, DOM, pagination, building coverage |
| `selftest` | opens one assigned `SELFTEST` issue and nothing else |

Inputs `forget_refs` (comma-separated refs) and `reset_ledger` force alerts for testing.

## What each issue title means

- `[wasl] NEW 2BR: ...` / `[wasl] N NEW 2BR listings` - act on it.
- `[wasl] RE-LISTED 2BR: ...` - a unit that had gone is available again.
- `[wasl] BASELINE ESTABLISHED` - first run; today's shortlist.
- `[wasl] LEDGER ANOMALY` - many "new" units at once; verify before trusting.
- `[wasl] MONITOR BROKEN` / `[wasl] WATCHDOG` - the monitor is not working; silence is
  not safe until it is closed as recovered.
- `[wasl] monitor status (heartbeat thread)` - daily "alive" comment.

## Changing the scope

Edit `config.yaml` -> `filter` (bedrooms, `max_rent_aed`, `community_contains`) and
`source.params`. Only parameters seen in real wasl URLs are used (`community`, `room`,
`usage`, `building`, `location`). See `docs/runbook.md`.

## Local development

```
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
ruff check . && pytest -q
python -m waslmon --mode dry-run      # needs network access to wasl.ae
sh scripts/install-hooks.sh
```
