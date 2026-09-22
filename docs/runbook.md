# Runbook

## Issue titles and what to do

| Title | Meaning | Action |
|---|---|---|
| `[wasl] NEW 2BR: <building> Unit <n> - AED <rent>` | A unit matching the criteria appeared | Open the link, enquire quickly |
| `[wasl] N NEW 2BR listings in wasl Village` | Several at once (max 10 listed, rest counted) | Same |
| `[wasl] RE-LISTED 2BR: ...` | A unit that disappeared is back | Same |
| `[wasl] BASELINE ESTABLISHED - N ...` | First run; everything matching today | Review the shortlist |
| `[wasl] LEDGER ANOMALY - N "new" ...` | > 15 "new" in one run; probably a ledger reset or site change | Check the Actions log before trusting |
| `[wasl] MONITOR BROKEN - N consecutive failures: <reason>` | Three failed runs in a row | Read the reason (below) |
| `[wasl] WATCHDOG - no successful monitor run for X h` | Runs not happening (dropped, disabled, quota) | Check Actions tab, run `monitor` manually |
| `[wasl] monitor status (heartbeat thread)` | Daily alive comment | Nothing. If it stops arriving, check the Actions tab |

## Failure reasons (log `RUN SUMMARY` -> `reason=`)

- `search_p1:http_status_403` / `challenge_title:` / `challenge_body_marker` - wasl or its
  edge is blocking the runner. Re-run once; if it persists the runner IP range is blocked.
  Options: hosted monitor service, or run from a machine in the UAE.
- `control_query_failed:total=...` - the site answers but not with real results.
- `counter_missing` / `counter_missing_and_no_listings` / `completeness_mismatch` / `pagination_param_unknown` -
  the page structure changed. Run `discovery`, read the log, update `config.yaml`
  (`extract.*`), run `dry-run`, then `monitor`.
- `parse_ratio_low` - fields still there but formatted differently; update `normalize.py`.
- `extractor_disagreement` - API and DOM paths disagree; inspect discovery output.
- `suspected_partial_page` - more than half the tracked units vanished in one run; the
  page was probably truncated. Nothing was written.
- `notify_error` - GitHub API failed; alerts stay pending and are retried next run.
- `ledger_corrupt` - restore with `git show HEAD~1:state/seen.json > state/seen.json`.

## Changing scope

`config.yaml`:

```yaml
# A: wasl Village 2BR any rent      -> filter.max_rent_aed: null
# B: wasl Village 2BR <= 60k (now)  -> as committed
# C: any Dubai 2BR <= 60k           -> remove source.params.community; filter.community_contains: null
```

Then run `dry-run`, check the table, then `monitor` with `reset_ledger=true` for a fresh
baseline.

## Forcing a test alert

Run `monitor` with `forget_refs=<one ref from state/seen.json>`. Exactly one NEW issue
should appear and `alerted_at` should be re-stamped in the ledger commit.

## Cadence and quota

Free plan: 2,000 Actions minutes/month, billed per job rounded up to the minute. The daily
heartbeat prints the month-to-date estimate. Hourly is fine when a run bills <= 2 minutes;
otherwise switch the cron to `37 */2 * * *`.
