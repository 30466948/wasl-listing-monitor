"""Independent liveness check (standard library only; runs without pip install).

Reads state/health.json from the checkout and raises an assigned issue when the
monitor has not succeeded for WATCHDOG_STALE_HOURS. It shares no fetch code with the
monitor, so a broken scraper cannot break the watchdog.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from .gh_api import GitHubAPIError, GitHubClient

WATCHDOG_PREFIX = "[wasl] WATCHDOG"


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _annot(kind: str, msg: str) -> None:
    prefix = f"::{kind}::" if os.environ.get("GITHUB_ACTIONS") else f"{kind.upper()}: "
    print(prefix + msg, flush=True)


def main() -> int:
    root = Path(os.environ.get("WASLMON_ROOT", "."))
    label = os.environ.get("WASLMON_HEALTH_LABEL", "monitor-health")
    stale_hours = float(os.environ.get("WATCHDOG_STALE_HOURS", "4"))
    path = root / "state" / "health.json"
    try:
        h = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        _annot("error", f"cannot read {path}: {e}")
        return 1
    if not h.get("armed_at"):
        _annot("notice", "watchdog: monitor not armed yet (no successful run recorded); nothing to check")
        return 0
    now = datetime.now(UTC)
    last = _parse(h.get("last_success_at"))
    age_h = (now - last).total_seconds() / 3600 if last else float("inf")
    try:
        client = GitHubClient.from_env()
    except GitHubAPIError as e:
        _annot("error", f"watchdog cannot reach GitHub: {e}")
        return 1
    server, repo = os.environ.get("GITHUB_SERVER_URL", "https://github.com"), os.environ.get("GITHUB_REPOSITORY", "")
    actions_url = f"{server}/{repo}/actions"
    if age_h > stale_hours:
        title = f"{WATCHDOG_PREFIX} - no successful monitor run for {age_h:.1f} h"
        body = (f"The last successful monitor run was **{age_h:.1f} h ago** "
                f"({h.get('last_success_at')}); threshold {stale_hours:g} h.\n\n"
                f"- consecutive failures: {h.get('consecutive_failures')}\n"
                f"- last run status: {h.get('last_run_status')} at {h.get('last_run_started_at')}\n"
                f"- last failure reason: `{h.get('last_failure_reason')}`\n\n"
                f"Possible causes: scheduled runs dropped or disabled, Actions quota exhausted, site blocking. "
                f"Check {actions_url} and trigger the monitor manually (workflow_dispatch). "
                f"Until it succeeds, silence does not mean no new listings.")
        try:
            issue, created = client.upsert_rolling_issue(label, title, body)
        except GitHubAPIError as e:
            _annot("error", f"watchdog could not open issue: {e}")
            return 1
        _annot("error", f"watchdog: STALE ({age_h:.1f} h). Issue {'opened' if created else 'updated'}: {issue.get('html_url')}")
        return 0
    try:
        for issue in client.list_open_issues(label):
            if str(issue.get("title", "")).startswith(WATCHDOG_PREFIX):
                client.comment(issue["number"], f"Recovered: last successful run {age_h:.1f} h ago at {now:%Y-%m-%d %H:%M} UTC. Closing.")
                client.close_issue(issue["number"])
                _annot("notice", f"watchdog: closed recovered issue #{issue['number']}")
    except GitHubAPIError as e:
        _annot("warning", f"watchdog could not close old issues: {e}")
    _annot("notice", f"watchdog: fresh, last success {age_h:.1f} h ago (threshold {stale_hours:g} h)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
