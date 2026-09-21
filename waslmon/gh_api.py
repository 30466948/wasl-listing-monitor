"""Minimal GitHub REST client on the standard library (shared by monitor and watchdog).

Issues assigned to the repository owner generate a 'participating' notification, which
GitHub emails regardless of watch state. Never rely on an unassigned issue.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Optional


class GitHubAPIError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"GitHub API {status}: {message}")
        self.status = status


class GitHubClient:
    def __init__(self, token: str, repo: str, api_url: Optional[str] = None, owner: Optional[str] = None):
        if not token:
            raise GitHubAPIError(0, "GITHUB_TOKEN is empty")
        if not repo or "/" not in repo:
            raise GitHubAPIError(0, "GITHUB_REPOSITORY must be owner/name")
        self.token = token
        self.repo = repo
        self.api = (api_url or os.environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
        self.owner = owner or os.environ.get("GITHUB_REPOSITORY_OWNER") or repo.split("/")[0]

    @classmethod
    def from_env(cls) -> GitHubClient:
        return cls(os.environ.get("GITHUB_TOKEN", ""), os.environ.get("GITHUB_REPOSITORY", ""))

    # -- transport ---------------------------------------------------------
    def _request(self, method: str, path: str, body: Any = None, retries: int = 2) -> Any:
        url = path if path.startswith("http") else f"{self.api}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "wasl-listing-monitor",
            "Content-Type": "application/json",
        })
        for attempt in range(retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as e:
                msg = e.read().decode("utf-8", "replace")[:300]
                if e.code in (500, 502, 503, 504) and attempt < retries:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise GitHubAPIError(e.code, msg) from None
            except urllib.error.URLError as e:
                if attempt < retries:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise GitHubAPIError(0, str(e.reason)) from None

    # -- issues --------------------------------------------------------------
    def ensure_label(self, name: str, color: str = "0e8a16", description: str = "") -> None:
        try:
            self._request("POST", f"/repos/{self.repo}/labels",
                          {"name": name, "color": color, "description": description[:100]})
        except GitHubAPIError as e:
            if e.status != 422:  # 422 = already exists
                raise

    def create_issue(self, title: str, body: str, labels: list[str], assign_owner: bool = True) -> dict:
        payload: dict[str, Any] = {"title": title[:250], "body": body[:60000], "labels": labels}
        if assign_owner:
            payload["assignees"] = [self.owner]
        return self._request("POST", f"/repos/{self.repo}/issues", payload)

    def list_open_issues(self, label: str) -> list[dict]:
        res = self._request("GET", f"/repos/{self.repo}/issues?state=open&labels={label}&per_page=20")
        return [i for i in (res or []) if "pull_request" not in i]

    def comment(self, number: int, body: str) -> dict:
        return self._request("POST", f"/repos/{self.repo}/issues/{number}/comments", {"body": body[:60000]})

    def close_issue(self, number: int) -> dict:
        return self._request("PATCH", f"/repos/{self.repo}/issues/{number}",
                             {"state": "closed", "state_reason": "completed"})

    def get_repo(self) -> dict:
        return self._request("GET", f"/repos/{self.repo}")

    def upsert_rolling_issue(self, label: str, title: str, body: str) -> tuple[dict, bool]:
        """Comment on the open issue with this label, or create one. Returns (issue, created)."""
        open_issues = self.list_open_issues(label)
        if open_issues:
            issue = open_issues[0]
            self.comment(issue["number"], body)
            return issue, False
        return self.create_issue(title, body, [label]), True
