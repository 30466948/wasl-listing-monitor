"""Alert rendering and delivery through GitHub Issues assigned to the repository owner."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Optional
from zoneinfo import ZoneInfo

from . import __version__, log
from .config import Settings
from .gh_api import GitHubClient
from .models import AlertKind, ListingRecord

LABEL_COLORS = {"alert": "0e8a16", "health": "b60205", "status": "0052cc"}


@dataclass
class RunInfo:
    run_url: str = ""
    breakdown: str = ""
    fetch_path: str = ""
    query_url: str = ""
    total_records: Optional[int] = None
    gap_hours: Optional[float] = None


def fmt_time(dt: Optional[datetime], tz: str) -> str:
    if dt is None:
        return "-"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    local = dt.astimezone(ZoneInfo(tz))
    return f"{local:%Y-%m-%d %H:%M} {tz} ({dt.astimezone(UTC):%H:%M} UTC)"


def bed_tag(settings: Settings) -> str:
    beds = settings.filter.bedrooms
    if not beds:
        return "listing"
    return "/".join("Studio" if b == 0 else f"{b}BR" for b in sorted(beds))


def community_name(settings: Settings) -> str:
    c = settings.filter.community_contains or settings.source.params.get("community") or "all communities"
    return c.replace("-", " ").title().replace("Wasl", "wasl")


def render_listing(rec: ListingRecord, tz: str) -> str:
    name = rec.building or rec.building_code or "Building ?"
    if rec.building_code and rec.building and rec.building_code not in rec.building:
        name = f"{rec.building} ({rec.building_code})"
    unit = f", Unit {rec.unit_no}" if rec.unit_no else ""
    beds = rec.bedrooms_raw or ("Studio" if rec.bedrooms == 0 else f"{rec.bedrooms} Bedroom" if rec.bedrooms else "bedrooms ?")
    rent = f"**AED {rec.rent_aed:,}** / yr" if rec.rent_aed is not None else f"rent: {rec.rent_raw or '?'}"
    size = f" - {rec.size_raw}" if rec.size_raw else ""
    loc = f" - {rec.location}" if rec.location else ""
    flag = "**Could not classify, check manually:** " if rec.unclassified else ""
    head = f"- {flag}**{name}{unit}** - {beds} - {rent}{size}{loc}"
    tail = f"  \n  [open listing]({rec.url}) - ref `{rec.ref}` - first seen {fmt_time(rec.first_seen, tz)}"
    if rec.alert_kind == "relisted" and rec.price_history and len(rec.price_history) > 1:
        tail += f" - previous rent AED {rec.price_history[-2].rent_aed:,}" if rec.price_history[-2].rent_aed else ""
    return head + tail


def alert_title(kind: AlertKind, records: list[ListingRecord], settings: Settings) -> str:
    n = len(records)
    bed = bed_tag(settings)
    if kind == "baseline":
        return f"[wasl] BASELINE ESTABLISHED - {n} matching {bed} listing{'s' if n != 1 else ''}"
    if kind == "anomaly":
        return f"[wasl] LEDGER ANOMALY - {n} \"new\" listings in one run, verify before trusting"
    word = "NEW" if kind == "new" else "RE-LISTED"
    if n == 1:
        return f"[wasl] {word} {bed}: {records[0].short_label()}"
    return f"[wasl] {n} {word} {bed} listings in {community_name(settings)}"


def render_alert_body(kind: AlertKind, records: list[ListingRecord], settings: Settings, info: RunInfo) -> str:
    tz = settings.notify.timezone
    cap = settings.notify.baseline_max_listings if kind in ("baseline", "anomaly") else settings.notify.max_listings_per_issue
    intro = {
        "baseline": ("This is the first run. Every listing below already matches your criteria today; "
                     "future issues will only report listings that are new or re-listed."),
        "new": "New listing(s) matching your criteria appeared on wasl.ae since the last check.",
        "relisted": "Listing(s) that had disappeared are available again.",
        "anomaly": ("An unusually large number of listings appeared at once. This usually means the ledger "
                    "was reset or the site changed, not that the market moved. Verify before trusting."),
    }[kind]
    lines = [intro, ""]
    lines.append(f"**Criteria:** {bed_tag(settings)}, rent <= AED {settings.filter.max_rent_aed:,}, {community_name(settings)}"
                 if settings.filter.max_rent_aed else f"**Criteria:** {bed_tag(settings)}, any rent, {community_name(settings)}")
    lines.append("")
    shown = sorted(records, key=lambda r: (r.rent_aed is None, r.rent_aed or 0))
    for rec in shown[:cap]:
        lines.append(render_listing(rec, tz))
    if len(shown) > cap:
        lines.append(f"- ... and {len(shown) - cap} more (see `state/seen.json`)")
    if info.gap_hours:
        lines += ["", f"**MISSED RUNS DETECTED:** {info.gap_hours} h since the previous check (expected about "
                  f"{settings.health.interval_hours} h)."]
    lines += ["", "---",
              f"Query: {info.query_url or settings.search_url()}  ",
              f"Site total for this query: {info.total_records if info.total_records is not None else '?'} - "
              f"{info.breakdown}  ",
              f"Fetch path: {info.fetch_path or '?'} - scraper v{__version__} - "
              f"[workflow run]({info.run_url})" if info.run_url else f"Fetch path: {info.fetch_path or '?'} - scraper v{__version__}"]
    return "\n".join(lines)


class Notifier:
    def __init__(self, settings: Settings, client: Optional[GitHubClient], info: RunInfo, dry_run: bool = False):
        self.settings = settings
        self.client = client
        self.info = info
        self.dry_run = dry_run
        self._labels_ready = False

    # -- internals ---------------------------------------------------------
    def _labels(self) -> None:
        if self._labels_ready or self.client is None:
            return
        lb = self.settings.notify.labels
        self.client.ensure_label(lb.alert, LABEL_COLORS["alert"], "new matching listing")
        self.client.ensure_label(lb.health, LABEL_COLORS["health"], "monitor broken / recovered / watchdog")
        self.client.ensure_label(lb.status, LABEL_COLORS["status"], "daily heartbeat")
        self._labels_ready = True

    def _deliver(self, title: str, body: str, label: str) -> Optional[str]:
        if self.dry_run or self.client is None:
            log.info(f"--- would open issue: {title}")
            for line in body.splitlines():
                log.info("    " + line)
            return None
        self._labels()
        issue = self.client.create_issue(title, body, [label])
        return issue.get("html_url")

    # -- public ------------------------------------------------------------
    def send_listings(self, kind: AlertKind, records: list[ListingRecord]) -> Optional[str]:
        title = alert_title(kind, records, self.settings)
        body = render_alert_body(kind, records, self.settings, self.info)
        return self._deliver(title, body, self.settings.notify.labels.alert)

    def send_health(self, title: str, body: str) -> Optional[str]:
        if self.dry_run or self.client is None:
            log.info(f"--- would upsert health issue: {title}")
            return None
        self._labels()
        issue, _ = self.client.upsert_rolling_issue(self.settings.notify.labels.health, title, body)
        return issue.get("html_url")

    def close_health(self, body: str) -> int:
        if self.dry_run or self.client is None:
            return 0
        n = 0
        for issue in self.client.list_open_issues(self.settings.notify.labels.health):
            if str(issue.get("title", "")).startswith("[wasl] MONITOR BROKEN"):
                self.client.comment(issue["number"], body)
                self.client.close_issue(issue["number"])
                n += 1
        return n

    def send_heartbeat(self, body: str) -> Optional[str]:
        if self.dry_run or self.client is None:
            log.info("--- would post heartbeat")
            return None
        self._labels()
        issue, _ = self.client.upsert_rolling_issue(self.settings.notify.labels.status,
                                                    "[wasl] monitor status (heartbeat thread)", body)
        return issue.get("html_url")

    def send_selftest(self) -> Optional[str]:
        title = "[wasl] SELFTEST - notification path OK"
        body = ("If you are reading this in your email inbox, the alert channel works: this issue was "
                "opened by the monitor and assigned to you.\n\n"
                "Next: confirm receipt to the builder so the schedule can be enabled.\n\n"
                f"Run: {self.info.run_url or '(local)'}")
        return self._deliver(title, body, self.settings.notify.labels.status)
