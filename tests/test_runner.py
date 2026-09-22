"""End-to-end run against the fixture page with all network replaced by fakes."""
import json

import pytest

from tests.test_gh_and_watchdog import FakeClient
from waslmon import runner
from waslmon.fetch import FetchResult
from waslmon.gh_api import GitHubAPIError


class FakeHttp:
    def __init__(self, settings, limiter, pages, control_total=905):
        self.pages = pages
        self.control_total = control_total
        self.calls = []

    def get(self, url, referer=None, prime=True):
        self.calls.append(url)
        if "usage=APARTMENT" in url and "community" not in url:
            html = f"<html><title>Search</title><body><span>1-12 of {self.control_total} records found</span>" + \
                   "".join(f'<a href="/en/unit/residential/IM0010000{i:04d}">x</a> AED 1' for i in range(12)) + "</body></html>"
            return FetchResult(url, 200, html, url, "text/html", [], [], 0.1, "requests")
        return FetchResult(url, 200, self.pages, url, "text/html", [], [], 0.1, "requests")


@pytest.fixture
def repo(tmp_path, settings, page_html, monkeypatch):
    (tmp_path / "state").mkdir()
    fake_gh = FakeClient()
    monkeypatch.setattr(runner.GitHubClient, "from_env", classmethod(lambda cls: fake_gh))
    http = {}

    def factory(s, limiter):
        http["obj"] = FakeHttp(s, limiter, page_html)
        return http["obj"]

    monkeypatch.setattr(runner, "HttpFetcher", factory)
    settings.health.heartbeat.cadence = "off"          # wall-clock dependent; tested in test_health
    env = {"GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": "o/r", "GITHUB_RUN_ID": "7"}
    return tmp_path, settings, fake_gh, env, http


def test_baseline_then_quiet_then_forced(repo):
    root, settings, gh, env, http = repo
    code = runner.run(settings, "monitor", root, env)
    assert code == 0
    ledger = json.loads((root / "state" / "seen.json").read_text())
    assert set(ledger["listings"]) == {"IM00100279345"}
    rec = ledger["listings"]["IM00100279345"]
    assert rec["baseline"] and rec["alerted_at"] and rec["alerted_via"] == "github_issue"
    assert ledger["meta"]["baseline_sent_at"]
    alerts = [i for i in gh.open if i["title"].startswith("[wasl] BASELINE") or i["title"].startswith("[wasl] NEW")]
    assert len(alerts) == 1 and alerts[0]["title"].startswith("[wasl] BASELINE ESTABLISHED - 1") and alerts[0]["assignees"] == ["o"]
    health = json.loads((root / "state" / "health.json").read_text())
    assert health["armed_at"] and health["last_run_status"] == "ok" and health["run_history"][0]["matched"] == 1
    assert (root / "state" / "structure.json").exists()

    # heartbeat happened (hour >= 4 UTC in most test runs) -> at most one status issue; count alert issues only
    n_before = len([i for i in gh.open if "BASELINE" in i["title"] or "NEW" in i["title"]])
    code = runner.run(settings, "monitor", root, env)
    assert code == 0
    assert len([i for i in gh.open if "BASELINE" in i["title"] or "NEW" in i["title"]]) == n_before  # quiet run

    env2 = dict(env, WASLMON_FORGET_REFS="IM00100279345")
    code = runner.run(settings, "monitor", root, env2)
    assert code == 0
    new_issues = [i for i in gh.open if i["title"].startswith("[wasl] NEW 2BR")]
    assert len(new_issues) == 1 and "R1083-35 Unit 101 - AED 47,000" in new_issues[0]["title"]


def test_dry_run_writes_nothing(repo):
    root, settings, gh, env, http = repo
    assert runner.run(settings, "dry-run", root, env) == 0
    assert not (root / "state" / "seen.json").exists() and not gh.open
    assert not (root / "state" / "health.json").exists()


def test_selftest_opens_assigned_issue(repo):
    root, settings, gh, env, http = repo
    assert runner.run(settings, "selftest", root, env) == 0
    assert gh.open[0]["title"].startswith("[wasl] SELFTEST") and gh.open[0]["assignees"] == ["o"]
    assert not (root / "state" / "seen.json").exists()


def test_degraded_on_challenge_and_broken_after_three(repo, challenge_html, monkeypatch):
    root, settings, gh, env, http = repo
    settings.fetch.strategy = "requests"

    def factory(s, limiter):
        return FakeHttp(s, limiter, challenge_html)

    monkeypatch.setattr(runner, "HttpFetcher", factory)
    for _ in range(3):
        assert runner.run(settings, "monitor", root, env) == 2
    assert not (root / "state" / "seen.json").exists()
    health = json.loads((root / "state" / "health.json").read_text())
    assert health["consecutive_failures"] == 3 and health["broken_alert_sent_at"]
    broken = [i for i in gh.open if i["title"].startswith("[wasl] MONITOR BROKEN")]
    assert len(broken) == 1
    # fourth failure within cooldown: no second issue
    assert runner.run(settings, "monitor", root, env) == 2
    assert len([i for i in gh.open if i["title"].startswith("[wasl] MONITOR BROKEN")]) == 1


def test_control_failure_is_degraded(repo, monkeypatch, page_html):
    root, settings, gh, env, http = repo

    def factory(s, limiter):
        return FakeHttp(s, limiter, page_html, control_total=3)

    monkeypatch.setattr(runner, "HttpFetcher", factory)
    assert runner.run(settings, "monitor", root, env) == 2
    assert not (root / "state" / "seen.json").exists()


def test_notify_failure_keeps_alert_pending(repo, monkeypatch):
    root, settings, gh, env, http = repo

    def boom(*a, **k):
        raise GitHubAPIError(500, "down")

    monkeypatch.setattr(gh, "create_issue", boom)
    assert runner.run(settings, "monitor", root, env) == 2
    ledger = json.loads((root / "state" / "seen.json").read_text())
    assert ledger["listings"]["IM00100279345"]["alerted_at"] is None   # retried next run
    monkeypatch.undo()
