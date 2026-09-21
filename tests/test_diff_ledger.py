import json
from datetime import timedelta

import pytest

from waslmon.diff import apply_diff, pending_alerts
from waslmon.extract import extract_dom
from waslmon.filters import build_records
from waslmon.ledger import compact, forget_refs, load_ledger, new_ledger, save_ledger
from waslmon.models import Degraded, LedgerCorrupt


def _records(page_html, settings, now):
    recs, _ = build_records(extract_dom(page_html, settings), now, settings)
    return recs


def test_baseline_then_new_then_removed_then_relisted(page_html, settings, now):
    settings.filter.max_rent_aed = None  # two matches: 279345 and 280227
    recs = _records(page_html, settings, now)
    ledger = new_ledger(settings, now)
    d = apply_diff(ledger, recs, now, settings.diff, baseline=True)
    assert d.baseline and not d.new and set(ledger.listings) == {"IM00100279345", "IM00100280227"}
    assert all(r.alert_kind == "baseline" and r.baseline for r in ledger.listings.values())
    assert {r.ref for r in pending_alerts(ledger)} == {"IM00100279345", "IM00100280227"}
    for r in ledger.listings.values():
        r.alerted_at = now

    # run 2: 280227 gone, unchanged 279345, price change on 279345
    t2 = now + timedelta(hours=1)
    recs2 = [r for r in _records(page_html, settings, t2) if r.ref == "IM00100279345"]
    recs2[0].rent_aed = 48000
    d2 = apply_diff(ledger, recs2, t2, settings.diff, baseline=False)
    assert d2.absent == ["IM00100280227"] and not d2.removed and not d2.new
    assert d2.price_changes == [("IM00100279345", 47000, 48000)]
    assert ledger.listings["IM00100279345"].rent_aed == 48000
    assert len(ledger.listings["IM00100279345"].price_history) == 2
    assert pending_alerts(ledger) == []

    # run 3: still gone -> removed after 2 misses
    t3 = t2 + timedelta(hours=1)
    d3 = apply_diff(ledger, recs2, t3, settings.diff, baseline=False)
    assert d3.removed == ["IM00100280227"] and ledger.listings["IM00100280227"].removed_at == t3

    # run 4: back -> relisted, pending alert
    t4 = t3 + timedelta(hours=1)
    recs4 = _records(page_html, settings, t4)
    d4 = apply_diff(ledger, recs4, t4, settings.diff, baseline=False)
    assert d4.relisted == ["IM00100280227"] and not d4.new
    assert ledger.listings["IM00100280227"].removed_at is None
    assert [r.ref for r in pending_alerts(ledger)] == ["IM00100280227"]


def test_new_ref_same_fingerprint_is_relisted(page_html, settings, now):
    recs = _records(page_html, settings, now)
    match = [r for r in recs if r.matched]
    ledger = new_ledger(settings, now)
    apply_diff(ledger, match, now, settings.diff, baseline=True)
    old = ledger.listings["IM00100279345"]
    old.removed_at = now
    twin = match[0].model_copy(deep=True)
    twin.ref = "IM00100999999"
    d = apply_diff(ledger, [twin], now + timedelta(hours=2), settings.diff, baseline=False)
    assert d.relisted == ["IM00100999999"] and d.new == []


def test_anomaly_guard(settings, now, page_html):
    recs = _records(page_html, settings, now)
    base = [r for r in recs if r.matched][0]
    many = []
    for i in range(20):
        c = base.model_copy(deep=True)
        c.ref = f"IM001000{i:05d}"
        c.fingerprint = f"fp{i}"
        many.append(c)
    ledger = new_ledger(settings, now)
    d = apply_diff(ledger, many, now, settings.diff, baseline=False)
    assert d.anomaly and all(r.alert_kind == "anomaly" for r in ledger.listings.values())
    ledger2 = new_ledger(settings, now)
    d2 = apply_diff(ledger2, many, now, settings.diff, baseline=True)
    assert not d2.anomaly


def test_mass_removal_guard(settings, now, page_html):
    recs = _records(page_html, settings, now)
    base = [r for r in recs if r.matched][0]
    many = []
    for i in range(10):
        c = base.model_copy(deep=True)
        c.ref = f"IM001000{i:05d}"
        c.fingerprint = f"fp{i}"
        many.append(c)
    ledger = new_ledger(settings, now)
    apply_diff(ledger, many, now, settings.diff, baseline=True)
    with pytest.raises(Degraded):
        apply_diff(ledger, many[:2], now + timedelta(hours=1), settings.diff, baseline=False)
    # nothing mutated
    assert all(r.absent_since is None for r in ledger.listings.values())


def test_rejected_records_not_stored(page_html, settings, now):
    recs = _records(page_html, settings, now)
    ledger = new_ledger(settings, now)
    apply_diff(ledger, recs, now, settings.diff, baseline=True)
    assert set(ledger.listings) == {"IM00100279345"}


def test_ledger_roundtrip_forget_compact_corrupt(tmp_path, page_html, settings, now):
    recs = _records(page_html, settings, now)
    ledger = new_ledger(settings, now)
    apply_diff(ledger, recs, now, settings.diff, baseline=True)
    path = tmp_path / "state" / "seen.json"
    save_ledger(path, ledger)
    back = load_ledger(path)
    assert back is not None and back.listings["IM00100279345"].rent_aed == 47000
    assert back.listings["IM00100279345"].first_seen == now
    assert load_ledger(tmp_path / "missing.json") is None
    assert forget_refs(back, ["im00100279345", "nope"]) == ["IM00100279345"]
    # compaction
    ledger.listings["IM00100279345"].removed_at = now - timedelta(days=100)
    n = compact(ledger, now, tmp_path / "state", 90)
    assert n == 1 and not ledger.listings
    archived = json.loads((tmp_path / "state" / f"archive-{now.year}.json").read_text())
    assert archived[0]["ref"] == "IM00100279345"
    # corrupt file raises, never empty fallback
    path.write_text("{not json")
    with pytest.raises(LedgerCorrupt):
        load_ledger(path)
    path.write_text(json.dumps({"schema_version": 1, "listings": {}}))
    with pytest.raises(LedgerCorrupt):
        load_ledger(path)
