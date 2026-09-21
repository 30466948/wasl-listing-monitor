from datetime import timedelta

from waslmon.health import (
    billed_min_est,
    broken_due,
    heartbeat_due,
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
from waslmon.models import Health, RunHistoryEntry


def _entry(now, status):
    return RunHistoryEntry(started_at=now, status=status, duration_s=30, billed_min_est=2)


def test_broken_and_recovered_edges(settings, now):
    h = Health()
    cfg = settings.health
    for i in range(3):
        t = now + timedelta(hours=i)
        start_run(h, t, cfg)
        record_failure(h, t, "search_p1:http_status_403", _entry(t, "degraded"))
        if i < 2:
            assert not broken_due(h, t, cfg)
    t = now + timedelta(hours=2)
    assert broken_due(h, t, cfg)
    mark_broken_sent(h, t)
    assert not broken_due(h, t + timedelta(hours=1), cfg)          # cooldown
    assert broken_due(h, t + timedelta(hours=25), cfg)              # repeats after cooldown
    t_ok = now + timedelta(hours=3)
    record_success(h, t_ok, _entry(t_ok, "ok"))
    assert h.armed_at == t_ok and h.consecutive_failures == 0
    assert recovered_due(h)
    mark_recovered_sent(h)
    assert not recovered_due(h) and h.broken_alert_sent_at is None


def test_gap_detection(settings, now):
    h = Health()
    start_run(h, now, settings.health)
    assert start_run(h, now + timedelta(hours=1), settings.health) is None
    gap = start_run(h, now + timedelta(hours=5), settings.health)
    assert gap == 4.0 and h.last_gap_hours == 4.0


def test_heartbeat_daily_and_weekly(settings, now):
    h = Health()
    cfg = settings.health
    early = now.replace(hour=2)
    assert not heartbeat_due(h, early, cfg)
    assert heartbeat_due(h, now, cfg)
    mark_heartbeat_sent(h, now, cfg)
    assert not heartbeat_due(h, now + timedelta(hours=3), cfg)
    assert heartbeat_due(h, now + timedelta(days=1), cfg)
    cfg.heartbeat.cadence = "weekly"
    cfg.heartbeat.weekday = now.weekday()
    h2 = Health()
    assert heartbeat_due(h2, now, cfg)
    mark_heartbeat_sent(h2, now, cfg)
    assert not heartbeat_due(h2, now + timedelta(days=7 - 7), cfg)
    assert not heartbeat_due(h2, now + timedelta(days=1), cfg)  # wrong weekday
    cfg.heartbeat.cadence = "off"
    assert not heartbeat_due(Health(), now, cfg)


def test_billing_and_persistence(tmp_path, now):
    assert billed_min_est(5) == 2 and billed_min_est(60) == 3 and billed_min_est(0) == 2
    h = Health()
    record_success(h, now, _entry(now, "ok"))
    record_success(h, now + timedelta(hours=1), _entry(now + timedelta(hours=1), "ok"))
    assert month_to_date_billed(h, now) == 4
    p = tmp_path / "health.json"
    save_health(p, h)
    assert load_health(p).last_success_at == now + timedelta(hours=1)
    p.write_text("garbage")
    assert load_health(p).armed_at is None  # advisory state, replaced not fatal
    assert load_health(tmp_path / "none.json").consecutive_failures == 0
