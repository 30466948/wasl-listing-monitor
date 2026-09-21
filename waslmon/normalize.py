"""Permissive parsing of the strings wasl shows on listing cards.

Evidenced variants (search snippets, 2026-09): bedrooms 'Studio', '1 Bedroom',
'2 Bedroom Large', '2 Bedrooms (extra large)', '3 Bedroom + Store'; rent 'AED 47,000',
'AED 185,999'; size '794', '2,112.00 sq.ft.', '1,084-1,190'; building
'wasl Village - Building 35' with physical code 'R1083-35'; unit 'Unit #101'.
Every parser returns None instead of raising; callers turn None into a DataIssue.
"""
from __future__ import annotations

import hashlib
import re
from typing import Optional

REF_RE = re.compile(r"IM\d{11}")
BUILDING_CODE_RE = re.compile(r"\bR\d{2,5}-[A-Za-z0-9]{1,8}\b")
_NUM = r"\d[\d,]*(?:\.\d+)?"
AED_RE = re.compile(rf"AED\s*({_NUM})(?:\s*(?:-|to)\s*({_NUM}))?", re.I)
SQFT_RE = re.compile(rf"({_NUM})(?:\s*-\s*({_NUM}))?\s*sq\.?\s*\.?\s*ft", re.I)
BED_RE = re.compile(r"(\d+)\s*(?:\+\s*[A-Za-z]+\s*)?(?:-\s*)?bed", re.I)
BED_LABEL_RE = re.compile(r"(Studio|\d+\s*(?:\+\s*[A-Za-z]+\s*)?-?\s*Bed(?:room)?s?(?:\s*\([^)]{0,30}\)|\s+(?:Large|Small|Extra\s+Large|\+\s*\w+))?)", re.I)
UNIT_RE = re.compile(r"\bunit\s*(?:no\.?|#|number)?\s*:?\s*#?\s*([A-Za-z0-9-]{1,10})\b", re.I)
BUILDING_RE = re.compile(r"((?:wasl\s+)?[A-Za-z][A-Za-z ]{1,30}?\s*-\s*Building\s+[A-Za-z0-9]{1,4}|Building\s+[A-Za-z0-9]{1,4})", re.I)
_WS = re.compile(r"\s+")


def norm_text(s: Optional[str]) -> Optional[str]:
    if s is None:
        return None
    s = _WS.sub(" ", str(s)).strip()
    return s or None


def _to_number(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    try:
        return float(s.replace(",", "").strip())
    except ValueError:
        return None


def parse_ref(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    m = REF_RE.search(text)
    return m.group(0) if m else None


def parse_bedrooms(raw: Optional[str]) -> Optional[int]:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if "studio" in s.lower():
        return 0
    m = BED_RE.search(s)
    if m:
        return int(m.group(1))
    m = re.match(r"\s*(\d+)\s*$", s)
    if m:
        return int(m.group(1))
    return None


def parse_aed(raw: Optional[str]) -> Optional[int]:
    """'AED 47,000' -> 47000; 'AED 45,000 - 50,000' -> 45000 (minimum of a range)."""
    if raw is None:
        return None
    s = str(raw)
    m = AED_RE.search(s)
    if m:
        nums = [_to_number(g) for g in m.groups() if g]
    else:
        nums = [_to_number(x) for x in re.findall(_NUM, s)]
    nums = [n for n in nums if n is not None and n > 0]
    if not nums:
        return None
    return int(round(min(nums)))


def parse_sqft(raw: Optional[str]) -> Optional[float]:
    if raw is None:
        return None
    s = str(raw)
    m = SQFT_RE.search(s)
    if m:
        nums = [_to_number(g) for g in m.groups() if g]
    else:
        nums = [_to_number(x) for x in re.findall(_NUM, s)]
    nums = [n for n in nums if n is not None and n > 0]
    if not nums:
        return None
    return float(min(nums))


def parse_building_code(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    m = BUILDING_CODE_RE.search(text)
    return m.group(0) if m else None


def parse_unit_no(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    m = UNIT_RE.search(text)
    if not m:
        return None
    val = m.group(1)
    # 'Unit Type' style false positives
    if val.lower() in {"type", "no", "number"}:
        return None
    return val


def parse_bedrooms_label(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    m = BED_LABEL_RE.search(text)
    return norm_text(m.group(1)) if m else None


def parse_building_name(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    m = BUILDING_RE.search(text)
    return norm_text(m.group(1)) if m else None


def fingerprint(ref: str, building_code: Optional[str], building: Optional[str],
                unit_no: Optional[str], unit_type: Optional[str], size_sqft: Optional[float]) -> str:
    """Secondary identity: same physical unit even if wasl mints a new reference."""
    parts = [
        (building_code or building or "").casefold(),
        (unit_no or "").casefold(),
        (unit_type or "").casefold(),
        str(int(size_sqft)) if size_sqft else "",
    ]
    if not any(parts[:2]):
        return f"ref:{ref}"
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


def content_hash(rent_aed: Optional[int], bedrooms_raw: Optional[str], size_raw: Optional[str],
                 building: Optional[str], unit_no: Optional[str]) -> str:
    key = "|".join([
        str(rent_aed or ""), (bedrooms_raw or "").casefold(), (size_raw or "").casefold(),
        (building or "").casefold(), (unit_no or "").casefold(),
    ])
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
