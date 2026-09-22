"""Identity-based diff of the current result set against the ledger."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .config import DiffSettings
from .models import Degraded, Ledger, ListingRecord, PricePoint


@dataclass
class DiffResult:
    new: list[str] = field(default_factory=list)
    relisted: list[str] = field(default_factory=list)
    price_changes: list[tuple[str, int | None, int | None]] = field(default_factory=list)
    absent: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    anomaly: bool = False
    baseline: bool = False

    @property
    def alert_count(self) -> int:
        return len(self.new) + len(self.relisted)


def apply_diff(ledger: Ledger, current: list[ListingRecord], now: datetime, cfg: DiffSettings,
               baseline: bool) -> DiffResult:
    """Mutates the ledger in memory. Raises Degraded on a suspected partial page."""
    res = DiffResult(baseline=baseline)
    current_by_ref = {r.ref: r for r in current}
    kept_now = {r.ref for r in current if r.keep}

    active_before = [r for r in ledger.listings.values() if r.removed_at is None]
    absent_now = [r for r in active_before if r.ref not in kept_now]
    if (len(active_before) >= cfg.mass_removal_min_tracked
            and len(absent_now) / len(active_before) > cfg.mass_removal_ratio):
        raise Degraded(f"suspected_partial_page:{len(absent_now)}_of_{len(active_before)}_tracked_absent")

    known_fp = {}
    for r in ledger.listings.values():
        known_fp.setdefault(r.fingerprint, []).append(r)

    for ref, cur in current_by_ref.items():
        existing = ledger.listings.get(ref)
        if existing is not None:
            was_removed = existing.removed_at is not None
            old_rent = existing.rent_aed
            _update_fields(existing, cur, now)
            if cur.keep:
                existing.last_seen = now
                existing.missed_runs = 0
                existing.absent_since = None
                if was_removed:
                    existing.removed_at = None
                    existing.alert_kind = "relisted"
                    existing.alerted_at = None
                    existing.alerted_via = None
                    res.relisted.append(ref)
                elif old_rent != cur.rent_aed and cur.rent_aed is not None:
                    res.price_changes.append((ref, old_rent, cur.rent_aed))
                    existing.price_history.append(PricePoint(at=now, rent_aed=cur.rent_aed))
            continue
        if not cur.keep:
            continue
        twins = [t for t in known_fp.get(cur.fingerprint, [])
                 if not cur.fingerprint.startswith("ref:") and (t.removed_at or t.absent_since)]
        rec = cur.model_copy(deep=True)
        rec.first_seen = now
        rec.last_seen = now
        if baseline:
            rec.baseline = True
            rec.alert_kind = "baseline"
        elif twins:
            rec.alert_kind = "relisted"
            res.relisted.append(ref)
        else:
            rec.alert_kind = "new"
            res.new.append(ref)
        ledger.listings[ref] = rec

    for r in absent_now:
        r.missed_runs += 1
        if r.absent_since is None:
            r.absent_since = now
        res.absent.append(r.ref)
        if r.missed_runs >= cfg.missing_runs_before_removed:
            r.removed_at = now
            res.removed.append(r.ref)

    if not baseline and res.alert_count > cfg.anomaly_new_threshold:
        res.anomaly = True
        for ref in res.new + res.relisted:
            ledger.listings[ref].alert_kind = "anomaly"
    return res


def _update_fields(existing: ListingRecord, cur: ListingRecord, now: datetime) -> None:
    for name in ("building", "building_code", "unit_no", "unit_type", "bedrooms", "bedrooms_raw",
                 "rent_aed", "rent_raw", "size_sqft", "size_raw", "community", "location", "url",
                 "matched", "unclassified", "reject_reasons", "content_hash", "extractor", "fingerprint"):
        val = getattr(cur, name)
        # classification outputs always move together; scraped fields keep last known value
        if name in ("matched", "unclassified", "reject_reasons") or (val is not None and val != []):
            setattr(existing, name, val)


def pending_alerts(ledger: Ledger) -> list[ListingRecord]:
    """Records discovered but not yet delivered (at-least-once delivery)."""
    return [r for r in ledger.listings.values()
            if r.alerted_at is None and r.keep and r.alert_kind is not None]
