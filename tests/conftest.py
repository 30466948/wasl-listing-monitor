from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from waslmon.config import Settings, load_settings

FIX = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def settings() -> Settings:
    return load_settings(ROOT / "config.yaml", {})


@pytest.fixture
def page_html() -> str:
    return (FIX / "search_page_min.html").read_text(encoding="utf-8")


@pytest.fixture
def challenge_html() -> str:
    return (FIX / "challenge_page.html").read_text(encoding="utf-8")


@pytest.fixture
def api_json() -> dict:
    return json.loads((FIX / "api_sample.json").read_text(encoding="utf-8"))


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 9, 21, 10, 37, tzinfo=UTC)
