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

from bs4 import BeautifulSoup

REF_RE = re.compile(r"IM\d{11}")
BUILDING_CODE_RE = re.compile(r"\bR\d{2,5}(?:-[A-Za-z0-9]{1,8})?\b")
_NUM = r"\d[\d,]*(?:\.\d+)?"
_PRICE = r"\d{1,3}(?:,\d{3})+|\d{4,}"   # price-shaped: 47,000 or 47000, never a bare "2"
AED_RE = re.compile(rf"AED\s*({_NUM})(?:\s*(?:-|to)\s*({_PRICE}))?", re.I)
SQFT_RE = re.compile(rf"({_NUM})(?:\s*-\s*({_NUM}))?\s*sq\.?\s*\.?\s*ft", re.I)
# wasl cards (2026-09): "Size (Sq.ft.) 1,355.00" - label first, number after
SIZE_LABEL_RE = re.compile(rf"Size\s*\(?\s*Sq\.?\s*ft\.?\s*\)?\s*:?\s*({_NUM})(?:\s*-\s*({_NUM}))?", re.I)
# wasl cards: "Price <dirham symbol> 101,999 / Year"; the symbol is a private-use glyph, so match any
# short non-digit run between the label and the amount
RENT_LABEL_RE = re.compile(r"(?:Price|Rent)\s*[^\d\n]{0,12}?(\d{1,3}(?:,\d{3})+|\d{4,})(?:\.\d+)?(\s*/\s*(?:Year|Yr|Month|Mo)\w*)?", re.I)
# wasl cards: "Type: 2 room flat", "Type: 3 bedroom", "Type: Studio"
TYPE_LABEL_RE = re.compile(r"Type\s*:\s*(Studio|\d+\s*(?:room|bed)\w*(?:\s+(?:flat|apartment|villa|large|small|\+\s*\w+))?)", re.I)
BED_RE = re.compile(r"(\d+)\s*(?:\+\s*[A-Za-z]+\s*)?(?:-\s*)?(?:bed|room)", re.I)
BED_LABEL_RE = re.compile(r"(Studio|\d+\s*(?:\+\s*[A-Za-z]+\s*)?-?\s*Bed(?:room)?s?(?:\s*\([^)]{0,30}\)|\s+(?:Large|Small|Extra\s+Large|\+\s*\w+))?)", re.I)
UNIT_RE = re.compile(r"\bunit\s*(?:no\.?|#|number)?\s*:?\s*#?\s*([A-Za-z0-9-]{1,10})\b", re.I)
BUILDING_RE = re.compile(r"((?:wasl\s+)?[A-Za-z][A-Za-z ]{1,30}?\s*-\s*Building\s+[A-Za-z0-9]{1,4}|Building\s+[A-Za-z0-9]{1,4})", re.I)
_WS = re.compile(r"\s+")


def visible_text(html: str) -> str:
    """Text a visitor would see: scripts, styles and markup removed."""
    soup = BeautifulSoup(html or "", "html.parser")
    for t in soup(["script", "style", "noscript", "template"]):
        t.decompose()
    return soup.get_text(" ", strip=True)


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
    """'AED 47,000' -> 47000; 'Price <sym> 101,999 / Year' -> 101999; 'AED 45,000 - 50,000' -> 45000.

    The first labelled amount is authoritative; a trailing unrelated number
    ('AED 47,000 Yearly Deposit AED 5,000', 'AED 47,000 2 Bedroom') never wins.
    """
    if raw is None:
        return None
    s = str(raw)
    m = AED_RE.search(s) or RENT_LABEL_RE.search(s)
    if m:
        lo = _to_number(m.group(1))
        return int(round(lo)) if lo and lo > 0 else None
    nums = [n for n in (_to_number(x) for x in re.findall(_NUM, s)) if n and n > 0]
    return int(round(nums[0])) if nums else None


def find_rent_text(text: Optional[str]) -> Optional[str]:
    """The rent phrase as shown on a card, for rent_raw."""
    if not text:
        return None
    m = AED_RE.search(text) or RENT_LABEL_RE.search(text)
    return norm_text(m.group(0)) if m else None


def find_size_text(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    m = SIZE_LABEL_RE.search(text) or SQFT_RE.search(text)
    return norm_text(m.group(0)) if m else None


def find_type_text(text: Optional[str]) -> Optional[str]:
    """Bedroom label: prefers wasl's 'Type: 2 room flat', falls back to '2 Bedroom Large' style."""
    if not text:
        return None
    m = TYPE_LABEL_RE.search(text)
    if m:
        return norm_text(m.group(1))
    return parse_bedrooms_label(text)


def parse_sqft(raw: Optional[str]) -> Optional[float]:
    if raw is None:
        return None
    s = str(raw)
    m = SIZE_LABEL_RE.search(s) or SQFT_RE.search(s)
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


_UNIT_LABELS = {"type", "no", "number", "details", "features", "size", "price", "ref", "reference"}


def parse_unit_no(text: Optional[str]) -> Optional[str]:
    """First 'Unit <n>' whose value contains a digit; label words ('Unit Type') are skipped."""
    if not text:
        return None
    for m in UNIT_RE.finditer(text):
        val = m.group(1)
        if val.lower() in _UNIT_LABELS or not any(c.isdigit() for c in val):
            continue
        return val
    return None


def parse_bedrooms_label(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    m = BED_LABEL_RE.search(text)
    return norm_text(m.group(1)) if m else None


# wasl card heading: "R441 - al barsha", "R1083-35 - wasl village"
HEADING_RE = re.compile(r"^\s*(R\d{2,5}(?:-[A-Za-z0-9]{1,8})?)\s*-\s*([A-Za-z][A-Za-z0-9 .'&-]{1,40}?)\s*$")


def parse_building_name(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    m = BUILDING_RE.search(text)
    return norm_text(m.group(1)) if m else None


def split_heading(heading: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """'R441 - al barsha' -> ('R441', 'al barsha'); anything else -> (code or None, None)."""
    h = norm_text(heading)
    if not h:
        return None, None
    m = HEADING_RE.match(h)
    if m:
        return m.group(1), norm_text(m.group(2))
    return parse_building_code(h), None


def fingerprint(ref: str, building_code: Optional[str], building: Optional[str],
                unit_no: Optional[str], unit_type: Optional[str], size_sqft: Optional[float]) -> str:
    """Secondary identity: same physical unit even if wasl mints a new reference."""
    parts = [
        (building_code or building or "").casefold(),
        (unit_no or "").casefold(),
        (unit_type or "").casefold(),
        str(int(size_sqft)) if size_sqft else "",
    ]
    # a fingerprint needs a building AND a unit-level discriminator, or it would collapse to
    # building level and make every unit in the building look like the same flat
    if not parts[0] or not (parts[1] or parts[2]):
        return f"ref:{ref}"
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


def content_hash(rent_aed: Optional[int], bedrooms_raw: Optional[str], size_raw: Optional[str],
                 building: Optional[str], unit_no: Optional[str]) -> str:
    key = "|".join([
        str(rent_aed or ""), (bedrooms_raw or "").casefold(), (size_raw or "").casefold(),
        (building or "").casefold(), (unit_no or "").casefold(),
    ])
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
