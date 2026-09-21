# wasl-listing-monitor - rules for Claude sessions

## Scope
Personal housing search helper for Shahin: detect new wasl Village 2-bedroom apartments
with rent <= AED 60,000 on wasl.ae and alert via assigned GitHub Issues. Nothing else.
Non-goals: applying or enquiring automatically, scraping other portals, storing anyone's
contact data.

## Stack (pinned, decision 2026-09-21)
- Python 3.12 in `mcr.microsoft.com/playwright/python:v1.62.0-noble`; `playwright==1.62.0`
  in requirements.txt MUST equal the image tag. Bump both in one commit.
- requests + beautifulsoup4 for the primary fetch path; Playwright only when the HTTP page
  lacks listing cards. No `actions/setup-python`, no `actions/cache`, no `playwright install`
  inside the container.
- pydantic v2 models for everything persisted. `state/seen.json` is validated on read; a
  validation failure is DEGRADED, never an empty-set fallback.

## Non-negotiable behaviours
1. **Fail loud, never silent.** A run may write the ledger only after proving the page is
   real (200, no challenge markers, counter parsed, control query total > 100, unique refs ==
   counter). Anything else exits DEGRADED (code 2) with the reason in the log.
2. **Only verified URL parameters** (`community`, `room`, `usage`, `building`, `location`).
   Rent cap, bedrooms and community are re-checked in Python on every record.
3. **Alert on identity**, never on sort order: unit ref `IM\d{11}` plus a content fingerprint.
   Two-phase delivery via `alerted_at`.
4. **Issues are always assigned to the owner.** GitHub no longer auto-watches new repos
   (May 2025); an unassigned issue emails nobody.
5. **Politeness:** <= 1 request/second, <= 40 requests per run, honour robots.txt, never poll
   the QA/mirror hosts (`qa2-web`, `prod1`, `notification.wasl.ae`) on a schedule.
6. **Redaction:** logs print URL param names only, header/cookie names only, redacted samples,
   lines <= 500 chars. Never dump raw HTML, HAR or cookies into the repo or logs.
7. **Never edit `.github/workflows/*` from the workflow** (GITHUB_TOKEN cannot). Workflow
   changes are human commits.
8. Detection latency is about one cron interval plus GitHub's queue delay. Say so; this is
   not a real-time system.

## Layout
`waslmon/` package (cli, config, fetch, extract, normalize, filters, ledger, diff, health,
gh_api, notify, discovery, runner, watchdog), `state/` committed by the bot,
`tests/` with fixtures, `docs/runbook.md`, `docs/data-source-decision.md`.

## Provenance
Every ledger record carries `first_seen`, `last_seen`, `extractor`, and the ledger meta
carries `query_url`, `scraper_version`, `last_fetch_path`. Do not remove these.
