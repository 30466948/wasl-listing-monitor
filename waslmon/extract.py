"""Extraction of listing cards from HTML (generic, then selector-tuned) or from JSON."""
from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any, Optional

from bs4 import BeautifulSoup
from bs4.element import Tag

from .config import Settings
from .fetch import CapturedResponse
from .models import DataIssue, Degraded, RawListing
from .normalize import (
    AED_RE,
    SQFT_RE,
    find_rent_text,
    find_size_text,
    find_type_text,
    norm_text,
    parse_building_code,
    parse_building_name,
    parse_ref,
    parse_unit_no,
    split_heading,
    visible_text,
)

__all__ = ["parse_counter", "visible_text", "has_no_results_marker", "dig", "find_record_arrays",
           "key_paths", "extract_api", "extract_dom", "reconcile", "structure_signature",
           "selector_chain", "card_for"]

UNIT_HREF_RE = re.compile(r"/unit/residential/(IM\d{11})", re.I)


def parse_counter(text: str, regex: str) -> Optional[tuple[int, int, int]]:
    """'1-12 of 905 records found' -> (1, 12, 905)."""
    if not text:
        return None
    m = re.search(regex, text, re.I)
    if not m:
        return None
    try:
        lo, hi, total = (int(g.replace(",", "")) for g in m.groups()[:3])
    except (ValueError, TypeError):
        return None
    return lo, hi, total


def has_no_results_marker(text: str, markers: Iterable[str]) -> bool:
    low = (text or "").lower()
    return any(m.lower() in low for m in markers)


# ---------------------------------------------------------------------------
# JSON path helpers
# ---------------------------------------------------------------------------
def dig(obj: Any, path: Optional[str]) -> Any:
    if path is None or path == "":
        return obj
    cur = obj
    for part in path.split("."):
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            if part not in cur:
                return None
            cur = cur[part]
        else:
            return None
    return cur


def find_record_arrays(obj: Any, path: str = "", depth: int = 0, max_depth: int = 4) -> list[tuple[str, list]]:
    """Discovery helper: every list-of-dicts inside a JSON document (path, list)."""
    out: list[tuple[str, list]] = []
    if depth > max_depth:
        return out
    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj[:5]):
            out.append((path or "$", obj))
        for i, v in enumerate(obj[:3]):
            out += find_record_arrays(v, f"{path}.{i}" if path else str(i), depth + 1, max_depth)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            out += find_record_arrays(v, f"{path}.{k}" if path else k, depth + 1, max_depth)
    return out


def key_paths(obj: Any, prefix: str = "", depth: int = 0, max_depth: int = 3) -> list[str]:
    if depth > max_depth or not isinstance(obj, dict):
        return []
    out: list[str] = []
    for k, v in obj.items():
        p = f"{prefix}.{k}" if prefix else str(k)
        out.append(p)
        if isinstance(v, dict):
            out += key_paths(v, p, depth + 1, max_depth)
    return sorted(out)


# ---------------------------------------------------------------------------
# API extraction (after discovery fills records_path / field_map)
# ---------------------------------------------------------------------------
def extract_api(captured: list[CapturedResponse], settings: Settings) -> Optional[tuple[list[RawListing], Optional[int]]]:
    api = settings.extract.api
    if not api.records_path or not api.field_map:
        return None
    for resp in captured:
        if resp.json is None:
            continue
        records = dig(resp.json, api.records_path)
        if not isinstance(records, list):
            continue
        total = dig(resp.json, api.total_path) if api.total_path else None
        out: list[RawListing] = []
        for rec in records:
            if not isinstance(rec, dict):
                continue
            fields: dict[str, Any] = {}
            for ours, theirs in api.field_map.items():
                val = dig(rec, theirs)
                if val is not None and ours in RawListing.model_fields:
                    fields[ours] = str(val) if not isinstance(val, str) else val
            ref = parse_ref(fields.get("ref")) or parse_ref(fields.get("url"))
            if not ref:
                continue
            fields["ref"] = ref
            fields.setdefault("url", settings.detail_url(ref))
            fields["extractor"] = "api"
            out.append(RawListing(**fields))
        try:
            total_int = int(str(total).replace(",", "")) if total is not None else None
        except ValueError:
            total_int = None
        return out, total_int
    return None


# ---------------------------------------------------------------------------
# DOM extraction
# ---------------------------------------------------------------------------
def _text(el: Tag) -> str:
    return el.get_text(" ", strip=True)


def selector_chain(el: Tag, levels: int = 4) -> str:
    parts: list[str] = []
    cur: Optional[Tag] = el
    while cur is not None and isinstance(cur, Tag) and len(parts) < levels:
        cls = ".".join(c for c in (cur.get("class") or []) if c)[:60]
        parts.append(cur.name + (f".{cls}" if cls else ""))
        cur = cur.parent if isinstance(cur.parent, Tag) else None
    return " < ".join(parts)


def card_for(anchor: Tag, max_levels: int = 8) -> Tag:
    """Nearest ancestor that reads like one listing card (contains a rent, is not huge)."""
    cur: Optional[Tag] = anchor
    best: Tag = anchor
    for _ in range(max_levels):
        if cur is None or not isinstance(cur.parent, Tag):
            break
        cur = cur.parent
        txt = _text(cur)
        if len(txt) > 4000:
            break
        n_refs = len(set(UNIT_HREF_RE.findall(str(cur))))
        if n_refs > 1:
            break
        best = cur
        if AED_RE.search(txt) and (SQFT_RE.search(txt) or re.search(r"bed|studio", txt, re.I)):
            return cur
    return best


def _first(card: Tag, selector: Optional[str]) -> Optional[str]:
    if not selector:
        return None
    try:
        el = card.select_one(selector)
    except Exception:
        return None
    return norm_text(_text(el)) if el else None


def extract_dom(html: str, settings: Settings) -> list[RawListing]:
    soup = BeautifulSoup(html or "", "html.parser")
    dom = settings.extract.dom
    cards: list[tuple[Tag, str]] = []
    if dom.card_selector:
        seen_sel: set[str] = set()
        for c in soup.select(dom.card_selector):
            m = UNIT_HREF_RE.search(str(c))
            if m and m.group(1).upper() not in seen_sel:   # grid and list views repeat each card
                seen_sel.add(m.group(1).upper())
                cards.append((c, m.group(1).upper()))
    else:
        seen: set[str] = set()
        for a in soup.select('a[href*="/unit/residential/"]'):
            m = UNIT_HREF_RE.search(a.get("href") or "")
            if not m:
                continue
            ref = m.group(1).upper()
            if ref in seen:
                continue
            seen.add(ref)
            cards.append((card_for(a), ref))

    out: list[RawListing] = []
    for card, ref in cards:
        txt = _text(card)
        f = dom.fields
        rent_raw = _first(card, f.get("rent_raw")) or find_rent_text(txt)
        bedrooms_raw = _first(card, f.get("bedrooms_raw")) or find_type_text(txt)
        size_raw = _first(card, f.get("size_raw")) or find_size_text(txt)
        heading = _first(card, f.get("building")) or _first(card, "h3") or _first(card, "h2")
        code_from_heading, community_from_heading = split_heading(heading)
        building = heading or parse_building_name(txt)
        building_code = _first(card, f.get("building_code")) or code_from_heading or parse_building_code(txt)
        unit_no = _first(card, f.get("unit_no")) or parse_unit_no(txt)
        community = _first(card, f.get("community")) or community_from_heading
        location = _first(card, f.get("location"))
        unit_type = _first(card, f.get("unit_type"))
        out.append(RawListing(
            ref=ref, url=settings.detail_url(ref), rent_raw=rent_raw, bedrooms_raw=bedrooms_raw,
            size_raw=size_raw, building=building, building_code=building_code, unit_no=unit_no,
            unit_type=unit_type, community=community, location=location, extractor="dom",
            card_text=txt[:300],
        ))
    return out


def reconcile(api_recs: Optional[list[RawListing]], dom_recs: list[RawListing],
              settings: Settings) -> tuple[list[RawListing], list[DataIssue]]:
    """Merge two extraction paths; they must agree on the set of refs."""
    issues: list[DataIssue] = []
    if not api_recs:
        return dom_recs, issues
    if not dom_recs:
        return api_recs, issues
    a = {r.ref for r in api_recs if r.ref}
    d = {r.ref for r in dom_recs if r.ref}
    union = a | d
    diff = a ^ d
    ratio = (len(diff) / len(union)) if union else 0.0
    if diff:
        issues.append(DataIssue(kind="disagreement",
                                detail=f"api/dom ref sets differ on {len(diff)} of {len(union)} ({ratio:.0%})"))
    if settings.extract.require_agreement and ratio > settings.extract.max_disagreement_ratio:
        raise Degraded(f"extractor_disagreement:{ratio:.0%}", issues)
    by_ref: dict[str, RawListing] = {r.ref: r for r in dom_recs if r.ref}
    merged: list[RawListing] = []
    for r in api_recs:
        if not r.ref:
            continue
        base = by_ref.pop(r.ref, None)
        if base is None:
            merged.append(r)
            continue
        data = base.model_dump()
        for k, v in r.model_dump().items():
            if v is not None and k != "extractor":
                data[k] = v
        data["extractor"] = "api"
        merged.append(RawListing(**data))
    merged.extend(by_ref.values())
    return merged, issues


def structure_signature(html: Optional[str], api_sample: Any = None) -> dict:
    sig: dict[str, Any] = {}
    if html:
        soup = BeautifulSoup(html, "html.parser")
        a = soup.select_one('a[href*="/unit/residential/"]')
        if a is not None:
            sig["dom_first_anchor_chain"] = selector_chain(a, 6)
            sig["dom_card_chain"] = selector_chain(card_for(a), 3)
        sig["dom_anchor_count"] = len(soup.select('a[href*="/unit/residential/"]'))
    if isinstance(api_sample, dict):
        sig["api_key_paths"] = key_paths(api_sample)[:80]
    return sig
