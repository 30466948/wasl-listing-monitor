"""Health state: consecutive failures, edge-triggered broken/recovered, heartbeat, gaps."""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from pydantic import ValidationError

from .config import HealthSettings
from .ledger import atomic_write_json
from .models import Health, RunHistoryEntry

MAX_HISTORY = 48
RUN_OVERHEAD_S = 70  # image pull + checkout + install the script cannot measure


def load_health(path: Path) -> Health:
    if not path.exists():
        return Health()
    try:
        return Health.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (ValueError, ValidationError):
        # health is advisory state; a corrupt file is replaced, never fatal
        return Health()


def save_health(path: Path, h: Health) -> None:
    atomic_write_json(path, h.model_dump(mode="json"))


def billed_min_est(duration_s: float) -> int:
    return max(1, math.ceil((duration_s + RUN_OVERHEAD_S) / 60))


def start_run(h: Health, now: datetime, cfg: HealthSettings) -> Optional[float]:
    """Record the start; return the gap in hours since the previous run if it is abnormal."""
    gap: Optional[float] = None
    if h.last_run_started_at is not None:
        hours = (now - h.last_run_started_at).total_seconds() / 3600
        if hours > cfg.gap_multiplier * cfg.interval_hours:
            gap = round(hours, 1)
    h.last_gap_hours = gap
    h.last_run_started_at = now
    h.interval_hours = cfg.interval_hours
    return gap


def _push_history(h: Health, entry: RunHistoryEntry) -> None:
    h.run_history.append(entry)
    if len(h.run_history) > MAX_HISTORY:
        del h.run_history[:-MAX_HISTORY]


def record_success(h: Health, now: datetime, entry: RunHistoryEntry) -> None:
    h.consecutive_failures = 0
    h.last_run_status = "ok"
    h.last_failure_reason = None
    h.last_success_at = now
    if h.armed_at is None:
        h.armed_at = now
    _push_history(h, entry)


def record_failure(h: Health, now: datetime, reason: str, entry: RunHistoryEntry) -> None:
    h.consecutive_failures += 1
    h.last_run_status = entry.status
    h.last_failure_reason = reason[:300]
    _push_history(h, entry)


def broken_due(h: Health, now: datetime, cfg: HealthSettings) -> bool:
    if h.consecutive_failures < cfg.fail_threshold:
        return False
    if h.broken_alert_sent_at is None:
        return True
    return now - h.broken_alert_sent_at >= timedelta(hours=cfg.broken_cooldown_hours)


def mark_broken_sent(h: Health, now: datetime) -> None:
    h.broken_alert_sent_at = now
    h.recovered_pending = True


def recovered_due(h: Health) -> bool:
    return h.recovered_pending and h.consecutive_failures == 0 and h.last_run_status == "ok"


def mark_recovered_sent(h: Health) -> None:
    h.recovered_pending = False
    h.broken_alert_sent_at = None


def heartbeat_due(h: Health, now: datetime, cfg: HealthSettings) -> bool:
    hb = cfg.heartbeat
    if hb.cadence == "off":
        return False
    if now.hour < hb.hour_utc:
        return False
    if hb.cadence == "weekly" and now.weekday() != hb.weekday:
        return False
    today = now.date().isoformat()
    if hb.cadence == "weekly":
        # once per ISO week
        key = f"{now.isocalendar().year}-W{now.isocalendar().week:02d}"
        return h.last_heartbeat_sent_on != key
    return h.last_heartbeat_sent_on != today


def mark_heartbeat_sent(h: Health, now: datetime, cfg: HealthSettings) -> None:
    if cfg.heartbeat.cadence == "weekly":
        h.last_heartbeat_sent_on = f"{now.isocalendar().year}-W{now.isocalendar().week:02d}"
    else:
        h.last_heartbeat_sent_on = now.date().isoformat()


def month_to_date_billed(h: Health, now: datetime) -> int:
    return sum(e.billed_min_est for e in h.run_history
               if e.started_at.year == now.year and e.started_at.month == now.month)


def history_line(h: Health, n: int = 12) -> str:
    marks = {"ok": "OK", "degraded": "DEG", "failed": "FAIL", "skipped": "SKIP"}
    return " ".join(marks.get(e.status, "?") for e in h.run_history[-n:]) or "-"
