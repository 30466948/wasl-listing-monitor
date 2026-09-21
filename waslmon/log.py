"""stdout logging with GitHub Actions annotations and a redaction-aware line limit."""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from datetime import UTC, datetime

from .redact import truncate

_IN_ACTIONS = bool(os.environ.get("GITHUB_ACTIONS"))


def _emit(line: str) -> None:
    sys.stdout.write(truncate(line) + "\n")
    sys.stdout.flush()


def info(msg: str) -> None:
    _emit(msg)


def notice(msg: str) -> None:
    _emit(f"::notice::{msg}" if _IN_ACTIONS else f"NOTICE: {msg}")


def warning(msg: str) -> None:
    _emit(f"::warning::{msg}" if _IN_ACTIONS else f"WARNING: {msg}")


def error(msg: str) -> None:
    _emit(f"::error::{msg}" if _IN_ACTIONS else f"ERROR: {msg}")


@contextmanager
def group(title: str):
    _emit(f"::group::{title}" if _IN_ACTIONS else f"=== {title} ===")
    try:
        yield
    finally:
        _emit("::endgroup::" if _IN_ACTIONS else f"=== end {title} ===")


def step_summary(markdown: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(markdown + "\n")
    except OSError:
        pass


def now_utc() -> datetime:
    return datetime.now(UTC)
