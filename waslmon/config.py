"""Settings: config.yaml validated with Pydantic, with WASLMON_* environment overrides."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import urlencode

import yaml
from pydantic import BaseModel, Field, ValidationError

from .models import ConfigError

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")


class SourceSettings(BaseModel):
    host: str = "https://www.wasl.ae"
    search_path: str = "/en/search/residential"
    params: dict[str, str] = Field(default_factory=lambda: {
        "community": "wasl-village", "room": "2", "usage": "APARTMENT"})
    control_params: dict[str, str] = Field(default_factory=lambda: {"usage": "APARTMENT"})
    control_min_total: int = 100
    user_agent: str = DEFAULT_UA
    min_seconds_between_requests: float = 1.0
    request_timeout_s: int = 30
    max_requests_per_run: int = 40


class FilterSettings(BaseModel):
    bedrooms: list[int] = Field(default_factory=lambda: [2])
    max_rent_aed: Optional[int] = 60000
    community_contains: Optional[str] = "wasl village"
    usage: Optional[str] = "apartment"
    include_unclassified: bool = True


class BrowserSettings(BaseModel):
    wait_selector: Optional[str] = None
    wait_timeout_s: int = 20
    api_url_pattern: Optional[str] = None


class FetchSettings(BaseModel):
    strategy: Literal["auto", "requests", "browser"] = "auto"
    browser: BrowserSettings = Field(default_factory=BrowserSettings)


class PaginationSettings(BaseModel):
    param: Optional[str] = None
    mode: Literal["page", "offset"] = "page"
    start: int = 1


class ApiSettings(BaseModel):
    records_path: Optional[str] = None
    total_path: Optional[str] = None
    field_map: dict[str, str] = Field(default_factory=dict)
    single_page: bool = False


class DomSettings(BaseModel):
    card_selector: Optional[str] = None
    fields: dict[str, str] = Field(default_factory=dict)


class ExtractSettings(BaseModel):
    counter_regex: str = r"(\d[\d,]*)\s*-\s*(\d[\d,]*)\s+of\s+(\d[\d,]*)\s+records?"
    no_results_markers: list[str] = Field(default_factory=lambda: [
        "no results", "no properties", "no records", "0 records found"])
    page_size: int = 12
    max_pages: int = 20
    pagination: PaginationSettings = Field(default_factory=PaginationSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)
    dom: DomSettings = Field(default_factory=DomSettings)
    require_agreement: bool = True
    max_disagreement_ratio: float = 0.10
    min_parse_ratio: float = 0.90


class DiffSettings(BaseModel):
    anomaly_new_threshold: int = 15
    mass_removal_ratio: float = 0.5
    mass_removal_min_tracked: int = 8
    missing_runs_before_removed: int = 2
    archive_after_days: int = 90


class HeartbeatSettings(BaseModel):
    cadence: Literal["daily", "weekly", "off"] = "daily"
    hour_utc: int = 4
    weekday: int = 0


class HealthSettings(BaseModel):
    fail_threshold: int = 3
    broken_cooldown_hours: int = 24
    interval_hours: float = 1.0
    gap_multiplier: float = 3.0
    heartbeat: HeartbeatSettings = Field(default_factory=HeartbeatSettings)


class LabelSettings(BaseModel):
    alert: str = "wasl-alert"
    health: str = "monitor-health"
    status: str = "monitor-status"
    selftest: str = "monitor-selftest"


class NotifySettings(BaseModel):
    channel: Literal["github_issue", "none"] = "github_issue"
    baseline: bool = True
    max_listings_per_issue: int = 10
    baseline_max_listings: int = 40
    labels: LabelSettings = Field(default_factory=LabelSettings)
    timezone: str = "Asia/Dubai"


class Settings(BaseModel):
    schema_version: int = 1
    source: SourceSettings = Field(default_factory=SourceSettings)
    filter: FilterSettings = Field(default_factory=FilterSettings)
    fetch: FetchSettings = Field(default_factory=FetchSettings)
    extract: ExtractSettings = Field(default_factory=ExtractSettings)
    diff: DiffSettings = Field(default_factory=DiffSettings)
    health: HealthSettings = Field(default_factory=HealthSettings)
    notify: NotifySettings = Field(default_factory=NotifySettings)

    # -- URL helpers -------------------------------------------------------
    def url_with_params(self, params: Mapping[str, str], page: Optional[int] = None,
                        page_size: Optional[int] = None) -> str:
        q = dict(params)
        pg = self.extract.pagination
        if page is not None and page > 1 and pg.param:
            if pg.mode == "page":
                q[pg.param] = str(pg.start + (page - 1))
            else:
                q[pg.param] = str(pg.start + (page - 1) * (page_size or self.extract.page_size))
        base = self.source.host.rstrip("/") + self.source.search_path
        return f"{base}?{urlencode(q)}" if q else base

    def search_url(self, page: Optional[int] = None, page_size: Optional[int] = None) -> str:
        return self.url_with_params(self.source.params, page, page_size)

    def control_url(self) -> str:
        return self.url_with_params(self.source.control_params)

    def detail_url(self, ref: str) -> str:
        return f"{self.source.host.rstrip('/')}/en/unit/residential/{ref}"


def _parse_int_list(s: str) -> list[int]:
    return [int(x) for x in s.split(",") if x.strip()]


def apply_env_overrides(data: dict, env: Mapping[str, str]) -> dict:
    """Runtime overrides used by workflow_dispatch tests. 'none' clears an optional value."""
    def sect(name: str) -> dict:
        return data.setdefault(name, {}) if isinstance(data.get(name, {}), dict) else data.setdefault(name, {})

    v = env.get("WASLMON_MAX_RENT")
    if v:
        sect("filter")["max_rent_aed"] = None if v.lower() == "none" else int(v)
    v = env.get("WASLMON_BEDROOMS")
    if v:
        sect("filter")["bedrooms"] = _parse_int_list(v)
    v = env.get("WASLMON_COMMUNITY")
    if v:
        src = sect("source")
        params = dict(src.get("params") or {})
        if v.lower() == "none":
            params.pop("community", None)
            sect("filter")["community_contains"] = None
        else:
            params["community"] = v
            sect("filter")["community_contains"] = v.replace("-", " ")
        src["params"] = params
    v = env.get("WASLMON_FETCH_STRATEGY")
    if v:
        sect("fetch")["strategy"] = v
    v = env.get("WASLMON_SOURCE_HOST")
    if v:
        sect("source")["host"] = v
    return data


def load_settings(path: Path, env: Optional[Mapping[str, str]] = None) -> Settings:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError as e:
        raise ConfigError(f"config not found: {path}") from e
    except yaml.YAMLError as e:
        raise ConfigError(f"config is not valid YAML: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping")
    raw = apply_env_overrides(raw, env or {})
    try:
        return Settings.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(f"config validation failed: {e}") from e
