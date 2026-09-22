import json
from datetime import UTC, datetime, timedelta

import pytest

from waslmon import gh_api, watchdog
from waslmon.gh_api import GitHubAPIError, GitHubClient


class FakeClient(GitHubClient):
    def __init__(self):
        super().__init__("t", "o/r", owner="o")
        self.calls = []
        self.open = []

    def _request(self, method, path, body=None, retries=2):
        self.calls.append((method, path, body))
        if method == "GET" and "/issues?" in path:
            return list(self.open)
        if method == "POST" and path.endswith("/issues"):
            issue = {"number": len(self.calls), "title": body["title"], "html_url": f"https://x/{len(self.calls)}",
                     "assignees": body.get("assignees")}
            self.open.append(issue)
            return issue
        if method == "POST" and path.endswith("/labels"):
            raise GitHubAPIError(422, "already_exists")
        if method == "PATCH":
            num = int(path.rsplit("/", 1)[1])
            self.open = [i for i in self.open if i["number"] != num]
            return {"number": num, "state": "closed"}
        return {}


def test_client_requires_env():
    with pytest.raises(GitHubAPIError):
        GitHubClient("", "o/r")
    with pytest.raises(GitHubAPIError):
        GitHubClient("t", "bad")


def test_create_issue_assigns_owner_and_rolling():
    c = FakeClient()
    c.ensure_label("x")  # 422 tolerated
    issue = c.create_issue("title v1", "b", ["l"])
    assert issue["assignees"] == ["o"]
    issue2, created = c.upsert_rolling_issue("monitor-health", "title v2", "body", title_prefix="title")
    assert not created and issue2["number"] == issue["number"]
    assert any(m == "POST" and p.endswith("/comments") for m, p, _ in c.calls)
    c.close_issue(issue["number"])
    _, created = c.upsert_rolling_issue("monitor-health", "title v3", "body", title_prefix="title")
    assert created
    _, created = c.upsert_rolling_issue("monitor-health", "other thread", "body", title_prefix="other")
    assert created                                    # different prefix, same label -> separate issue


def test_watchdog_paths(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    hp = state / "health.json"
    monkeypatch.setenv("WASLMON_ROOT", str(tmp_path))
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("WATCHDOG_STALE_HOURS", "4")
    fake = FakeClient()
    monkeypatch.setattr(gh_api.GitHubClient, "from_env", classmethod(lambda cls: fake))
    monkeypatch.setattr(watchdog.GitHubClient, "from_env", classmethod(lambda cls: fake))

    hp.write_text(json.dumps({"armed_at": None}))
    assert watchdog.main() == 0 and not fake.open           # not armed -> no-op

    now = datetime.now(UTC)
    hp.write_text(json.dumps({"armed_at": "2026-01-01T00:00:00Z",
                              "last_success_at": (now - timedelta(hours=9)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                              "consecutive_failures": 5, "last_run_status": "degraded",
                              "last_failure_reason": "search_p1:http_status_403"}))
    assert watchdog.main() == 0
    assert len(fake.open) == 1 and fake.open[0]["title"].startswith("[wasl] WATCHDOG")

    hp.write_text(json.dumps({"armed_at": "2026-01-01T00:00:00Z",
                              "last_success_at": now.strftime("%Y-%m-%dT%H:%M:%SZ")}))
    assert watchdog.main() == 0
    assert not fake.open                                     # closed on recovery
