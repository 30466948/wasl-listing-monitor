"""Turn RawListing into ListingRecord and classify it against the user's criteria."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .config import FilterSettings, Settings
from .models import ListingRecord, RawListing
from .normalize import content_hash, fingerprint, norm_text, parse_aed, parse_bedrooms, parse_building_code, parse_sqft


@dataclass
class Breakdown:
    fetched: int = 0
    matched: int = 0
    unclassified: int = 0
    rejected: dict[str, int] = field(default_factory=dict)

    def add_reject(self, reason: str) -> None:
        self.rejected[reason] = self.rejected.get(reason, 0) + 1

    @property
    def rejected_total(self) -> int:
        return sum(self.rejected.values())

    def summary(self) -> str:
        rej = ", ".join(f"{k}={v}" for k, v in sorted(self.rejected.items())) or "none"
        return (f"fetched={self.fetched} matched={self.matched} unclassified={self.unclassified} "
                f"rejected={self.rejected_total} [{rej}]")


def classify(rec: ListingRecord, f: FilterSettings) -> tuple[bool, bool, list[str]]:
    """Return (matched, unclassified, reject_reasons). Never trusts the URL filters."""
    reasons: list[str] = []
    unknown = False
    if rec.bedrooms is None:
        unknown = True
    elif rec.bedrooms not in f.bedrooms:
        reasons.append("wrong_bedrooms")
    if f.max_rent_aed is not None:
        if rec.rent_aed is None:
            unknown = True
        elif rec.rent_aed > f.max_rent_aed:
            reasons.append("over_price")
    if f.community_contains:
        hay = " ".join(x for x in (rec.community, rec.building, rec.building_code, rec.location) if x).casefold()
        if hay and f.community_contains.casefold() not in hay:
            reasons.append("wrong_community")
    if reasons:
        return False, False, reasons
    if unknown:
        return False, f.include_unclassified, ["unparsed_fields"] if not f.include_unclassified else []
    return True, False, []


def build_record(raw: RawListing, now: datetime, settings: Settings) -> Optional[ListingRecord]:
    if not raw.ref:
        return None
    bedrooms = parse_bedrooms(raw.bedrooms_raw)
    rent = parse_aed(raw.rent_raw)
    size = parse_sqft(raw.size_raw)
    building_code = raw.building_code or parse_building_code(raw.building)
    rec = ListingRecord(
        ref=raw.ref,
        fingerprint=fingerprint(raw.ref, building_code, raw.building, raw.unit_no, raw.unit_type, size),
        url=raw.url or settings.detail_url(raw.ref),
        building=norm_text(raw.building), building_code=building_code,
        unit_no=norm_text(raw.unit_no), unit_type=norm_text(raw.unit_type),
        bedrooms=bedrooms, bedrooms_raw=norm_text(raw.bedrooms_raw),
        rent_aed=rent, rent_raw=norm_text(raw.rent_raw),
        size_sqft=size, size_raw=norm_text(raw.size_raw),
        community=norm_text(raw.community), location=norm_text(raw.location),
        first_seen=now, last_seen=now, extractor=raw.extractor,
    )
    rec.content_hash = content_hash(rent, rec.bedrooms_raw, rec.size_raw, rec.building, rec.unit_no)
    matched, unclassified, reasons = classify(rec, settings.filter)
    rec.matched, rec.unclassified, rec.reject_reasons = matched, unclassified, reasons
    if rent is not None:
        from .models import PricePoint
        rec.price_history = [PricePoint(at=now, rent_aed=rent)]
    return rec


def build_records(raws: list[RawListing], now: datetime, settings: Settings) -> tuple[list[ListingRecord], Breakdown]:
    bd = Breakdown()
    out: list[ListingRecord] = []
    for raw in raws:
        rec = build_record(raw, now, settings)
        if rec is None:
            continue
        bd.fetched += 1
        if rec.matched:
            bd.matched += 1
        elif rec.unclassified:
            bd.unclassified += 1
        else:
            for r in rec.reject_reasons or ["rejected"]:
                bd.add_reject(r)
        out.append(rec)
    return out, bd
