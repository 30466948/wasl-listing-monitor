"""Regression tests for the defects found in the adversarial review."""
import json
from datetime import UTC, datetime, timedelta

import pytest

from tests.test_gh_and_watchdog import FakeClient
from tests.test_runner import FakeHttp
from waslmon import runner
from waslmon.diff import apply_diff, pending_alerts
from waslmon.extract import extract_dom
from waslmon.fetch import looks_like_results_page
from waslmon.filters import build_records
from waslmon.gh_api import GitHubAPIError
from waslmon.ledger import new_ledger
from waslmon.normalize import fingerprint, parse_aed, parse_unit_no
from waslmon.notify import Notifier, RunInfo
from waslmon.redact import redact_record


def test_missing_github_client_is_fatal_before_any_state(tmp_path, settings, monkeypatch):
    (tmp_path / "state").mkdir()

    def boom(cls):
        raise GitHubAPIError(0, "GITHUB_TOKEN is empty")

    monkeypatch.setattr(runner.GitHubClient, "from_env", classmethod(boom))
    assert runner.run(settings, "monitor", tmp_path, {}) == 3
    assert not (tmp_path / "state" / "seen.json").exists()
    assert not (tmp_path / "state" / "health.json").exists()
    assert runner.run(settings, "selftest", tmp_path, {}) == 3


def test_notifier_without_client_raises_instead_of_pretending(settings):
    n = Notifier(settings, None, RunInfo(), dry_run=False)
    with pytest.raises(GitHubAPIError):
        n.send_selftest()
    with pytest.raises(GitHubAPIError):
        n.send_health("t", "b")
    assert Notifier(settings, None, RunInfo(), dry_run=True).send_selftest() is None


def test_counter_missing_with_listings_is_degraded(tmp_path, settings, page_html, monkeypatch):
    (tmp_path / "state").mkdir()
    monkeypatch.setattr(runner.GitHubClient, "from_env", classmethod(lambda cls: FakeClient()))
    no_counter = page_html.replace("1-3 of 3 records found", "")
    monkeypatch.setattr(runner, "HttpFetcher", lambda s, limiter: FakeHttp(s, limiter, no_counter))
    assert runner.run(settings, "monitor", tmp_path, {}) == 2
    assert not (tmp_path / "state" / "seen.json").exists()
    health = json.loads((tmp_path / "state" / "health.json").read_text())
    assert health["last_failure_reason"].startswith("search_p1:not_a_results_page") or \
        health["last_failure_reason"] == "counter_missing"


def test_interrupted_run_is_recorded_as_failure(tmp_path, settings, monkeypatch):
    (tmp_path / "state").mkdir()
    monkeypatch.setattr(runner.GitHubClient, "from_env", classmethod(lambda cls: FakeClient()))

    def interrupt(*a, **k):
        raise KeyboardInterrupt()

    monkeypatch.setattr(runner, "gather", interrupt)
    with pytest.raises(KeyboardInterrupt):
        runner.run(settings, "monitor", tmp_path, {})
    health = json.loads((tmp_path / "state" / "health.json").read_text())
    assert health["last_run_status"] == "failed" and health["last_success_at"] is None
    assert health["last_failure_reason"].startswith("interrupted:")


def test_health_saved_even_if_notification_crashes(tmp_path, settings, page_html, monkeypatch):
    (tmp_path / "state").mkdir()
    monkeypatch.setattr(runner.GitHubClient, "from_env", classmethod(lambda cls: FakeClient()))
    monkeypatch.setattr(runner, "HttpFetcher", lambda s, limiter: FakeHttp(s, limiter, page_html))

    def crash(*a, **k):
        raise ValueError("boom")

    monkeypatch.setattr(runner, "_health_notifications", crash)
    assert runner.run(settings, "monitor", tmp_path, {}) == 0
    assert (tmp_path / "state" / "health.json").exists()


def test_pending_alert_survives_removal(page_html, settings, now):
    recs, _ = build_records(extract_dom(page_html, settings), now, settings)
    ledger = new_ledger(settings, now)
    apply_diff(ledger, recs, now, settings.diff, baseline=True)          # alerted_at stays None (delivery failed)
    for _ in range(2):
        apply_diff(ledger, [], now + timedelta(hours=1), settings.diff, baseline=False)
    rec = ledger.listings["IM00100279345"]
    assert rec.removed_at is not None
    assert [r.ref for r in pending_alerts(ledger)] == ["IM00100279345"]


def test_chunked_delivery_stamps_only_delivered_batches(tmp_path, settings, page_html, monkeypatch):
    (tmp_path / "state").mkdir()
    gh = FakeClient()
    monkeypatch.setattr(runner.GitHubClient, "from_env", classmethod(lambda cls: gh))
    settings.notify.max_listings_per_issue = 5
    settings.notify.baseline_max_listings = 5
    settings.health.heartbeat.cadence = "off"
    base_recs, _ = build_records(extract_dom(page_html, settings), datetime.now(UTC), settings)
    base = [r for r in base_recs if r.matched][0]
    many = []
    for i in range(12):
        c = base.model_copy(deep=True)
        c.ref = f"IM001000{i:05d}"
        c.fingerprint = f"fp{i}"
        c.rent_aed = 40000 + i * 100
        many.append(c)

    class Col:
        def __init__(self):
            self.raws, self.total, self.pages, self.path, self.first_html = [], 12, 1, "requests", ""
            self.issues, self.control_total, self.api_sample = [], 905, None

    monkeypatch.setattr(runner, "gather", lambda s: Col())
    monkeypatch.setattr(runner, "build_records", lambda raws, now, s: (many, runner.Breakdown(fetched=12, matched=12)))
    calls = {"n": 0}
    real_create = gh.create_issue

    def flaky_create(title, body, labels, assign_owner=True):
        calls["n"] += 1
        if calls["n"] == 3:
            raise GitHubAPIError(502, "bad gateway")
        return real_create(title, body, labels, assign_owner)

    monkeypatch.setattr(gh, "create_issue", flaky_create)
    assert runner.run(settings, "monitor", tmp_path, {}) == 2          # third batch failed -> degraded
    ledger = json.loads((tmp_path / "state" / "seen.json").read_text())
    stamped = [r for r in ledger["listings"].values() if r["alerted_at"]]
    pending = [r for r in ledger["listings"].values() if not r["alerted_at"]]
    assert len(stamped) == 10 and len(pending) == 2
    assert len([i for i in gh.open if "BASELINE" in i["title"]]) == 2


def test_label_query_is_encoded():
    c = FakeClient()
    c.list_open_issues("monitor health & more")
    path = c.calls[-1][1]
    assert "labels=monitor%20health%20%26%20more" in path and "per_page=100" in path


def test_parse_aed_ignores_trailing_numbers():
    assert parse_aed("AED 47,000 Yearly Deposit AED 5,000") == 47000
    assert parse_aed("AED 47,000 2 Bedroom") == 47000
    assert parse_aed("AED 45,000 - 50,000") == 45000
    assert parse_aed("AED 45,000 to 50,000") == 45000


def test_unit_no_skips_labels_and_needs_digit():
    assert parse_unit_no("Unit Type F1B Unit #101") == "101"
    assert parse_unit_no("Unit Details Unit No: 305") == "305"
    assert parse_unit_no("Unit Type Apartment") is None


def test_fingerprint_needs_unit_level_discriminator():
    assert fingerprint("IM1", "R1083-35", None, None, None, 794.0) == "ref:IM1"
    assert fingerprint("IM1", "R1083-35", None, "101", None, None) == fingerprint("IM2", "R1083-35", None, "101", None, None)


def test_markers_only_on_visible_text(settings):
    html = "<html><title>x</title><script>var s='no results';</script><body><p>Loading</p></body></html>"
    assert not looks_like_results_page(html, settings.extract.counter_regex, settings.extract.no_results_markers)
    html2 = "<html><title>x</title><body><p>No results for your search</p></body></html>"
    assert looks_like_results_page(html2, settings.extract.counter_regex, settings.extract.no_results_markers)


def test_redaction_keys():
    out = redact_record({"agentName": "x", "owner_phone": "1", "building_name": "wasl Village", "unit_name": "101",
                         "apiKey": "k", "card_text": "AED 47,000", "landlord": "y", "unitRef": "IM00100279345"})
    assert out["agentName"] == "<redacted>" and out["owner_phone"] == "<redacted>" and out["apiKey"] == "<redacted>"
    assert out["landlord"] == "<redacted>"
    assert out["building_name"] == "wasl Village" and out["unit_name"] == "101"
    assert out["card_text"] == "AED 47,000" and out["unitRef"] == "IM00100279345"


def test_rolling_issue_prefix_separation():
    c = FakeClient()
    c.create_issue("[wasl] WATCHDOG - no successful monitor run for 9.0 h", "b", ["monitor-health"])
    issue, created = c.upsert_rolling_issue("monitor-health", "[wasl] MONITOR BROKEN - 3 failures", "b",
                                            title_prefix="[wasl] MONITOR BROKEN")
    assert created and issue["title"].startswith("[wasl] MONITOR BROKEN")
    issue2, created2 = c.upsert_rolling_issue("monitor-health", "[wasl] MONITOR BROKEN - 4 failures", "b",
                                              title_prefix="[wasl] MONITOR BROKEN")
    assert not created2 and issue2["number"] == issue["number"]
