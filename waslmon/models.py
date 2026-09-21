"""Pydantic v2 models for records, the committed ledger and health state."""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

IssueKind = Literal[
    "fetch_error", "challenge", "parse_error", "completeness", "disagreement",
    "notify_error", "ledger", "config", "structure_drift", "other",
]
RunStatus = Literal["ok", "degraded", "failed", "skipped"]
AlertKind = Literal["baseline", "new", "relisted", "anomaly"]


class DataIssue(BaseModel):
    kind: IssueKind
    detail: str
    ref: Optional[str] = None

    def __str__(self) -> str:
        tail = f" (ref {self.ref})" if self.ref else ""
        return f"[{self.kind}] {self.detail}{tail}"


class Degraded(Exception):
    """The run cannot be trusted. No ledger write, health failure recorded."""

    def __init__(self, reason: str, issues: Optional[list[DataIssue]] = None):
        super().__init__(reason)
        self.reason = reason
        self.issues = issues or []


class LedgerCorrupt(Exception):
    pass


class ConfigError(Exception):
    pass


class RawListing(BaseModel):
    """One listing as extracted from the page or the JSON API, strings untouched."""

    ref: Optional[str] = None
    url: Optional[str] = None
    rent_raw: Optional[str] = None
    bedrooms_raw: Optional[str] = None
    size_raw: Optional[str] = None
    building: Optional[str] = None
    building_code: Optional[str] = None
    unit_no: Optional[str] = None
    unit_type: Optional[str] = None
    community: Optional[str] = None
    location: Optional[str] = None
    usage: Optional[str] = None
    extractor: Literal["api", "dom"] = "dom"
    card_text: Optional[str] = None


class PricePoint(BaseModel):
    at: datetime
    rent_aed: Optional[int] = None


class ListingRecord(BaseModel):
    ref: str
    fingerprint: str
    url: str
    building: Optional[str] = None
    building_code: Optional[str] = None
    unit_no: Optional[str] = None
    unit_type: Optional[str] = None
    bedrooms: Optional[int] = None
    bedrooms_raw: Optional[str] = None
    rent_aed: Optional[int] = None
    rent_raw: Optional[str] = None
    size_sqft: Optional[float] = None
    size_raw: Optional[str] = None
    community: Optional[str] = None
    location: Optional[str] = None
    matched: bool = False
    unclassified: bool = False
    reject_reasons: list[str] = Field(default_factory=list)
    content_hash: str = ""
    first_seen: datetime
    last_seen: datetime
    absent_since: Optional[datetime] = None
    removed_at: Optional[datetime] = None
    missed_runs: int = 0
    alerted_at: Optional[datetime] = None
    alerted_via: Optional[str] = None
    alert_kind: Optional[AlertKind] = None
    baseline: bool = False
    price_history: list[PricePoint] = Field(default_factory=list)
    extractor: str = "dom"

    @property
    def keep(self) -> bool:
        """Only matched or unclassified records are persisted."""
        return self.matched or self.unclassified

    def short_label(self) -> str:
        b = self.building_code or self.building or "?"
        u = f" Unit {self.unit_no}" if self.unit_no else ""
        rent = f"AED {self.rent_aed:,}" if self.rent_aed is not None else (self.rent_raw or "rent ?")
        return f"{b}{u} - {rent}"


class LedgerMeta(BaseModel):
    query_url: str
    scraper_version: str
    created_at: datetime
    source_host: str
    baseline_sent_at: Optional[datetime] = None
    last_success_at: Optional[datetime] = None
    last_fetch_path: Optional[str] = None
    last_total_records: Optional[int] = None


class Ledger(BaseModel):
    schema_version: int = 1
    meta: LedgerMeta
    listings: dict[str, ListingRecord] = Field(default_factory=dict)


class RunHistoryEntry(BaseModel):
    started_at: datetime
    status: RunStatus
    reason: Optional[str] = None
    fetched: int = 0
    matched: int = 0
    new: int = 0
    duration_s: float = 0.0
    billed_min_est: int = 0
    fetch_path: Optional[str] = None


class Health(BaseModel):
    schema_version: int = 1
    armed_at: Optional[datetime] = None
    interval_hours: float = 1.0
    consecutive_failures: int = 0
    last_run_started_at: Optional[datetime] = None
    last_run_status: Optional[RunStatus] = None
    last_failure_reason: Optional[str] = None
    last_success_at: Optional[datetime] = None
    broken_alert_sent_at: Optional[datetime] = None
    recovered_pending: bool = False
    last_heartbeat_sent_on: Optional[str] = None
    last_gap_hours: Optional[float] = None
    run_history: list[RunHistoryEntry] = Field(default_factory=list)
