from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from waslmon.config import Settings, load_settings

FIX = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def wasl_settings() -> Settings:
    """The committed production configuration (wasl card selectors, browser strategy)."""
    return load_settings(ROOT / "config.yaml", {})


@pytest.fixture
def settings() -> Settings:
    """Generic-mode settings for the synthetic fixture: no card selector, HTTP strategy."""
    s = load_settings(ROOT / "config.yaml", {})
    s.extract.dom.card_selector = None
    s.extract.dom.fields = {}
    s.fetch.strategy = "auto"
    return s


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
