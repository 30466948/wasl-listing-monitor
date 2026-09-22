"""Run orchestration: fetch -> assert health -> extract -> filter -> diff -> notify -> commit state."""
from __future__ import annotations

import math
import time
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Optional

import requests

from . import __version__, log
from .config import Settings
from .diff import DiffResult, apply_diff, pending_alerts
from .extract import (
    extract_api,
    extract_dom,
    has_no_results_marker,
    parse_counter,
    reconcile,
    structure_signature,
)
from .fetch import (
    BrowserFetcher,
    FetchResult,
    HttpFetcher,
    RateLimiter,
    RequestBudgetExceeded,
    detect_challenge,
    waf_fingerprint,
)
from .filters import Breakdown, build_records
from .gh_api import GitHubAPIError, GitHubClient
from .health import (
    billed_min_est,
    broken_due,
    heartbeat_due,
    history_line,
    load_health,
    mark_broken_sent,
    mark_heartbeat_sent,
    mark_recovered_sent,
    month_to_date_billed,
    record_failure,
    record_success,
    recovered_due,
    save_health,
    start_run,
)
from .ledger import atomic_write_json, compact, forget_refs, load_ledger, new_ledger, save_ledger
from .models import DataIssue, Degraded, Ledger, LedgerCorrupt, ListingRecord, RawListing, RunHistoryEntry
from .notify import Notifier, RunInfo, fmt_time
from .redact import safe_url


@dataclass
class Paths:
    root: Path
    state_dir: Path
    seen: Path
    health: Path
    structure: Path

    @classmethod
    def at(cls, root: Path) -> Paths:
        state = root / "state"
        return cls(root=root, state_dir=state, seen=state / "seen.json", health=state / "health.json",
                   structure=state / "structure.json")


@dataclass
class Collected:
    raws: list[RawListing]
    total: Optional[int]
    pages: int
    path: str
    first_html: str
    issues: list[DataIssue] = field(default_factory=list)
    control_total: Optional[int] = None
    api_sample: object = None


def run_url_from_env(env: Mapping[str, str]) -> str:
    server, repo, rid = env.get("GITHUB_SERVER_URL"), env.get("GITHUB_REPOSITORY"), env.get("GITHUB_RUN_ID")
    return f"{server}/{repo}/actions/runs/{rid}" if server and repo and rid else ""


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------
def _assert_page(r: FetchResult, settings: Settings, what: str) -> None:
    reason = detect_challenge(r, settings.extract.counter_regex, settings.extract.no_results_markers)
    fp = waf_fingerprint(r)
    if fp:
        log.info(f"{what}: edge fingerprint names={fp}")
    if reason:
        raise Degraded(f"{what}:{reason}", [DataIssue(kind="challenge" if "challenge" in reason else "fetch_error",
                                                        detail=f"{what} {reason} url={safe_url(r.final_url)}")])


def collect(settings: Settings, fetch_page: Callable[[str], FetchResult], path: str) -> Collected:
    """Walk every result page; the counter is mandatory unless the page says explicitly 'no results'."""
    ex = settings.extract
    issues: list[DataIssue] = []
    first = fetch_page(settings.search_url(1))
    _assert_page(first, settings, "search_p1")
    text = first.page_text()
    counter = parse_counter(text, ex.counter_regex)
    no_results = has_no_results_marker(text, ex.no_results_markers)
    api_sample = None

    def extract_one(r: FetchResult) -> list[RawListing]:
        nonlocal api_sample
        dom = extract_dom(r.text, settings)
        api = extract_api(r.captured, settings)
        api_recs = api[0] if api else None
        if api and api_sample is None:
            for c in r.captured:
                if c.json is not None:
                    api_sample = c.json
                    break
        merged, iss = reconcile(api_recs, dom, settings)
        issues.extend(iss)
        return merged

    raws = extract_one(first)
    if counter is None:
        if no_results and not raws:
            log.info("search_p1: explicit no-results marker present, zero listings")
            return Collected(raws=[], total=0, pages=1, path=path, first_html=first.text, issues=issues)
        raise Degraded("counter_missing" if raws else "counter_missing_and_no_listings",
                       [DataIssue(kind="completeness",
                                  detail="results counter not parsed; completeness cannot be asserted "
                                         f"({len(raws)} listing anchors seen)")])

    lo, hi, total = counter
    page_size = max(1, hi - lo + 1)
    pages = max(1, math.ceil(total / page_size)) if total else 1
    if pages > ex.max_pages:
        raise Degraded(f"too_many_pages:{pages}>{ex.max_pages}")
    if pages > 1 and not ex.pagination.param and not ex.api.single_page:
        raise Degraded(f"pagination_param_unknown:{total}_records_over_{pages}_pages",
                       [DataIssue(kind="completeness", detail="set extract.pagination.param after discovery")])
    if not ex.api.single_page:
        for p in range(2, pages + 1):
            r = fetch_page(settings.search_url(p, page_size))
            _assert_page(r, settings, f"search_p{p}")
            raws.extend(extract_one(r))

    seen: dict[str, RawListing] = {}
    for raw in raws:
        if raw.ref and raw.ref not in seen:
            seen[raw.ref] = raw
    uniq = list(seen.values())
    if len(uniq) != total:
        raise Degraded(f"completeness_mismatch:{len(uniq)}_refs_vs_{total}_records",
                       [DataIssue(kind="completeness", detail=f"collected {len(uniq)} unique refs, site says {total}")])
    return Collected(raws=uniq, total=total, pages=pages, path=path, first_html=first.text, issues=issues,
                     api_sample=api_sample)


def control_check(settings: Settings, fetch_page: Callable[[str], FetchResult]) -> int:
    r = fetch_page(settings.control_url())
    _assert_page(r, settings, "control")
    counter = parse_counter(r.page_text(), settings.extract.counter_regex)
    total = counter[2] if counter else r.ref_count
    if total < settings.source.control_min_total:
        raise Degraded(f"control_query_failed:total={total}<{settings.source.control_min_total}",
                       [DataIssue(kind="fetch_error", detail="broad control query returned too few records; "
                                                             "site or edge is not serving real results")])
    return total


def gather(settings: Settings) -> Collected:
    """Requests first, Chromium second (strategy auto), then the control query on the same path."""
    src = settings.source
    limiter = RateLimiter(src.min_seconds_between_requests, src.max_requests_per_run)
    http = HttpFetcher(settings, limiter)
    strategy = settings.fetch.strategy
    if strategy in ("auto", "requests"):
        probe = http.get(settings.search_url(1))
        reason = detect_challenge(probe, settings.extract.counter_regex, settings.extract.no_results_markers)
        if reason is None:
            log.info(f"http path OK (status {probe.status}, {len(probe.text)} bytes, {probe.ref_count} refs)")
            col = collect(settings, lambda u: http.get(u), "requests")
            col.control_total = control_check(settings, lambda u: http.get(u))
            return col
        if strategy == "requests":
            raise Degraded(f"http:{reason}", [DataIssue(kind="fetch_error", detail=reason)])
        log.warning(f"http path unusable ({reason}); falling back to headless browser")
    b = settings.fetch.browser
    with BrowserFetcher(settings, limiter) as bf:
        def fp(u: str) -> FetchResult:
            return bf.get(u, b.wait_selector, settings.extract.counter_regex,
                          settings.extract.no_results_markers, b.wait_timeout_s)
        col = collect(settings, fp, "browser")
        col.control_total = control_check(settings, fp)
        return col


# ---------------------------------------------------------------------------
# Rendering helpers for logs
# ---------------------------------------------------------------------------
def table(records: list[ListingRecord]) -> list[str]:
    lines = ["ref            | beds(raw->n)                 | rent(raw->aed)          | size      | building / unit                | verdict"]
    for r in sorted(records, key=lambda x: (not x.matched, x.rent_aed or 10**9)):
        verdict = "MATCH" if r.matched else ("UNCLASSIFIED" if r.unclassified else "reject:" + ",".join(r.reject_reasons))
        lines.append(f"{r.ref} | {str(r.bedrooms_raw)[:20]:<20}->{str(r.bedrooms):>3} | "
                     f"{str(r.rent_raw)[:14]:<14}->{str(r.rent_aed):>7} | {str(r.size_raw)[:9]:<9} | "
                     f"{(r.building_code or r.building or '?')[:20]:<20} {('U' + r.unit_no) if r.unit_no else '':<8} | {verdict}")
    return lines


def heartbeat_body(ledger: Ledger, health, settings: Settings, info: RunInfo, now: datetime) -> str:
    tz = settings.notify.timezone
    active = [r for r in ledger.listings.values() if r.removed_at is None and r.keep]
    day = now - timedelta(hours=24)
    new24 = [r for r in active if r.first_seen >= day]
    removed24 = [r for r in ledger.listings.values() if r.removed_at and r.removed_at >= day]
    lines = [f"Monitor alive at {fmt_time(now, tz)}.", "",
             f"- Tracked matching listings: **{len(active)}**",
             f"- New in last 24 h: {len(new24)}  -  removed in last 24 h: {len(removed24)}",
             f"- Last {min(12, len(health.run_history))} runs: `{history_line(health)}`",
             f"- Fetch path: {info.fetch_path}  -  site total for query: {info.total_records}",
             f"- Month-to-date billed minutes (estimate): {month_to_date_billed(health, now)}",
             f"- Query: {settings.search_url()}"]
    if info.gap_hours:
        lines.insert(1, f"**MISSED RUNS DETECTED:** {info.gap_hours} h between checks.")
    if active:
        lines += ["", "Current shortlist (cheapest first):"]
        for r in sorted(active, key=lambda x: (x.rent_aed is None, x.rent_aed or 0))[:15]:
            lines.append(f"- {r.short_label()} - [{r.ref}]({r.url})")
    return "\n".join(lines)


def _deliver_pending(ledger: Ledger, notifier: Notifier, settings: Settings, now: datetime,
                     issues: list[DataIssue], sent: list[str]) -> bool:
    """Send every pending alert in capped batches; stamp only records whose issue exists. Returns ok."""
    ok = True
    pend = pending_alerts(ledger)
    for kind in ("baseline", "anomaly", "new", "relisted"):
        recs = [r for r in pend if r.alert_kind == kind]
        if not recs:
            continue
        if kind == "baseline" and not settings.notify.baseline:
            for r in recs:
                r.alerted_at, r.alerted_via = now, "silent"
            continue
        cap = settings.notify.baseline_max_listings if kind in ("baseline", "anomaly") else settings.notify.max_listings_per_issue
        order = sorted(recs, key=lambda r: (r.rent_aed is None, r.rent_aed or 0))
        try:
            for i in range(0, len(order), cap):
                batch = order[i:i + cap]
                url = notifier.send_listings(kind, batch)
                if not url:
                    raise GitHubAPIError(0, "issue creation returned no URL")
                for r in batch:
                    r.alerted_at, r.alerted_via = now, "github_issue"
                sent.append(f"{kind}:{len(batch)} -> {url}")
            if kind == "baseline":
                ledger.meta.baseline_sent_at = now
        except GitHubAPIError as e:
            ok = False
            issues.append(DataIssue(kind="notify_error", detail=f"{kind} alert failed: {e}"))
            log.error(f"notification failed for {kind}: {e}")
    return ok


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------
def run(settings: Settings, mode: str, root: Path, env: Mapping[str, str]) -> int:
    now = datetime.now(UTC)
    paths = Paths.at(root)
    dry_run = mode == "dry-run"
    info = RunInfo(run_url=run_url_from_env(env), query_url=settings.search_url())
    client: Optional[GitHubClient] = None
    if not dry_run and settings.notify.channel == "github_issue":
        try:
            client = GitHubClient.from_env()
        except GitHubAPIError as e:
            # Fail loud before any state is touched: without a channel, nothing may be marked delivered.
            log.error(f"GitHub client unavailable, refusing to run: {e}")
            return 3
    notifier = Notifier(settings, client, info, dry_run=dry_run)

    if mode == "selftest":
        url = notifier.send_selftest()
        log.notice(f"SELFTEST issue opened: {url}")
        return 0

    health = load_health(paths.health)
    gap = start_run(health, now, settings.health) if not dry_run else None
    info.gap_hours = gap
    t0 = time.monotonic()
    status = "ok"
    reason: Optional[str] = None
    issues: list[DataIssue] = []
    bd = Breakdown()
    diff: Optional[DiffResult] = None
    ledger: Optional[Ledger] = None
    col: Optional[Collected] = None
    sent: list[str] = []

    try:
        ledger = load_ledger(paths.seen)
        if str(env.get("WASLMON_RESET_LEDGER", "")).lower() == "true":
            log.warning("WASLMON_RESET_LEDGER=true: ledger discarded, this run re-baselines")
            ledger = None
        if ledger is not None and env.get("WASLMON_FORGET_REFS"):
            dropped = forget_refs(ledger, env["WASLMON_FORGET_REFS"].split(","))
            log.warning(f"forgot refs (forced alert test): {dropped}")
        baseline = ledger is None

        col = gather(settings)
        info.fetch_path, info.total_records = col.path, col.total
        issues.extend(col.issues)
        records, bd = build_records(col.raws, now, settings)
        info.breakdown = bd.summary()
        if records:
            parsed = sum(1 for r in records if r.bedrooms is not None and r.rent_aed is not None
                         and (r.building or r.building_code))
            ratio = parsed / len(records)
            if ratio < settings.extract.min_parse_ratio:
                raise Degraded(f"parse_ratio_low:{ratio:.0%}",
                               [DataIssue(kind="parse_error", detail=f"only {parsed}/{len(records)} records parsed "
                                                                     "ref+bedrooms+rent+building")])
        if ledger is None:
            ledger = new_ledger(settings, now)
        diff = apply_diff(ledger, records, now, settings.diff, baseline)
        ledger.meta.last_success_at = now
        ledger.meta.last_fetch_path = col.path
        ledger.meta.last_total_records = col.total
        ledger.meta.scraper_version = __version__

        with log.group("PARSED"):
            for line in table(records):
                log.info(line)
            log.info(bd.summary())

        if dry_run:
            with log.group("DRY-RUN: alerts that a monitor run would send now"):
                pend = pending_alerts(ledger)
                if not pend:
                    log.info("(none)")
                for kind in ("baseline", "anomaly", "new", "relisted"):
                    recs = [r for r in pend if r.alert_kind == kind]
                    if recs:
                        notifier.send_listings(kind, recs)  # prints only
            return 0

        save_ledger(paths.seen, ledger)                        # phase 1: discovery is durable
        atomic_write_json(paths.structure, structure_signature(col.first_html, col.api_sample))
        delivered_ok = _deliver_pending(ledger, notifier, settings, now, issues, sent)
        save_ledger(paths.seen, ledger)                        # phase 2: delivery stamps are durable
        try:
            archived = compact(ledger, now, paths.state_dir, settings.diff.archive_after_days)
            if archived:
                log.info(f"archived {archived} old removed records")
                save_ledger(paths.seen, ledger)                # phase 3: housekeeping, best effort
        except Exception as e:  # noqa: BLE001 - housekeeping must never cost a delivery stamp
            issues.append(DataIssue(kind="ledger", detail=f"compaction failed: {type(e).__name__}"))
            log.warning(f"compaction failed: {type(e).__name__}: {str(e)[:120]}")
        if not delivered_ok:
            status, reason = "degraded", "notify_error"
    except Degraded as d:
        status, reason = "degraded", d.reason
        issues.extend(d.issues)
        log.error(f"DEGRADED: {d.reason}")
    except LedgerCorrupt as e:
        status, reason = "degraded", "ledger_corrupt"
        issues.append(DataIssue(kind="ledger", detail=str(e)))
        log.error(str(e))
    except (RequestBudgetExceeded, requests.RequestException, RuntimeError) as e:
        status, reason = "degraded", f"fetch_exception:{type(e).__name__}"
        issues.append(DataIssue(kind="fetch_error", detail=f"{type(e).__name__}: {str(e)[:200]}"))
        log.error(f"DEGRADED: {reason}: {str(e)[:200]}")
    except Exception as e:  # noqa: BLE001 - a crash must still record health
        status, reason = "failed", f"crash:{type(e).__name__}:{str(e)[:120]}"
        log.error(reason)
        for line in traceback.format_exc().splitlines()[-12:]:
            log.info(line)
    except BaseException as e:  # cancellation / SIGINT must never be recorded as success
        status, reason = "failed", f"interrupted:{type(e).__name__}"
        log.error(f"INTERRUPTED: {reason}")
        raise
    finally:
        duration = time.monotonic() - t0
        if not dry_run:
            entry = RunHistoryEntry(started_at=now, status=status, reason=reason, fetched=bd.fetched,
                                    matched=bd.matched, new=(diff.alert_count if diff else 0),
                                    duration_s=round(duration, 1), billed_min_est=billed_min_est(duration),
                                    fetch_path=info.fetch_path or None)
            if status == "ok":
                record_success(health, now, entry)
            else:
                record_failure(health, now, reason or status, entry)
            try:
                _health_notifications(settings, notifier, health, ledger, info, now, status, reason, issues)
            except Exception as e:  # noqa: BLE001 - health must be persisted even if notifying fails
                log.error(f"health notification crashed: {type(e).__name__}: {str(e)[:120]}")
            save_health(paths.health, health)
        _summary(mode, status, reason, info, bd, diff, issues, sent, duration, col)
    return {"ok": 0, "degraded": 2}.get(status, 1)


def _health_notifications(settings, notifier: Notifier, health, ledger, info, now, status, reason, issues) -> None:
    tz = settings.notify.timezone
    try:
        if broken_due(health, now, settings.health):
            body = (f"The monitor has failed **{health.consecutive_failures}** consecutive times.\n\n"
                    f"- Last failure: `{reason}`\n- Last success: {fmt_time(health.last_success_at, tz)}\n"
                    f"- Issues: " + "; ".join(str(i) for i in issues[:8]) + "\n\n"
                    f"Until this is fixed, **silence does not mean no new listings**. "
                    f"See docs/runbook.md. Run: {info.run_url}")
            url = notifier.send_health(f"[wasl] MONITOR BROKEN - {health.consecutive_failures} consecutive failures: "
                                       f"{(reason or '')[:60]}", body)
            mark_broken_sent(health, now)
            log.notice(f"BROKEN issue: {url}")
        elif recovered_due(health):
            n = notifier.close_health(f"Recovered at {fmt_time(now, tz)}: run succeeded "
                                      f"({info.breakdown}). Closing.")
            mark_recovered_sent(health)
            log.notice(f"RECOVERED: closed {n} broken issue(s)")
        if status == "ok" and ledger is not None and heartbeat_due(health, now, settings.health):
            url = notifier.send_heartbeat(heartbeat_body(ledger, health, settings, info, now))
            mark_heartbeat_sent(health, now, settings.health)
            log.notice(f"heartbeat posted: {url}")
    except GitHubAPIError as e:
        log.error(f"health notification failed: {e}")


def _summary(mode, status, reason, info: RunInfo, bd: Breakdown, diff, issues, sent, duration, col) -> None:
    lines = [f"mode={mode} status={status.upper()}" + (f" reason={reason}" if reason else ""),
             (f"fetch_path={info.fetch_path or '-'} site_total={info.total_records} pages={col.pages if col else '-'} "
              f"control_total={col.control_total if col else '-'}"),
             bd.summary(),
             (f"events: new={len(diff.new)} relisted={len(diff.relisted)} price_changes={len(diff.price_changes)} "
              f"absent={len(diff.absent)} removed={len(diff.removed)} anomaly={diff.anomaly} baseline={diff.baseline}")
             if diff else "events: -",
             f"notifications: {sent or '-'}",
             f"issues({len(issues)}): " + ("; ".join(str(i) for i in issues[:20]) or "-"),
             f"duration={duration:.1f}s billed_min_est={billed_min_est(duration)}"]
    with log.group("RUN SUMMARY"):
        for line in lines:
            log.info(line)
    log.step_summary("### wasl monitor run\n\n```\n" + "\n".join(lines) + "\n```")
