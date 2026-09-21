"""Redaction helpers.

Job logs are retained for 90 days and readable by anyone with repo access, so nothing
that could be a credential or a personal detail is printed. Rules: URLs are printed as
scheme://host/path?param_names_only; header and cookie NAMES only; dictionary values
under sensitive keys are replaced; every line is truncated.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlsplit

MAX_LINE = 500
SENSITIVE_KEY = re.compile(
    r"email|phone|mobile|(?<!building)(?<!community)(?<!location)(?<!unit)name|token|cookie|"
    r"auth|password|secret|session|address|contact|passport|emirates|\beid\b|iban|card",
    re.I,
)


def truncate(s: Any, n: int = MAX_LINE) -> str:
    s = str(s)
    return s if len(s) <= n else s[: n - 3] + "..."


def safe_url(url: str) -> str:
    """scheme://host/path?name1&name2 - query VALUES are never printed."""
    try:
        parts = urlsplit(url)
    except Exception:
        return "<unparseable-url>"
    names = sorted({k for k, _ in parse_qsl(parts.query, keep_blank_values=True)})
    q = ("?" + "&".join(names)) if names else ""
    return f"{parts.scheme}://{parts.netloc}{parts.path}{q}"


def param_names(url: str) -> list[str]:
    try:
        return sorted({k for k, _ in parse_qsl(urlsplit(url).query, keep_blank_values=True)})
    except Exception:
        return []


def redact_record(obj: Any, depth: int = 0, max_depth: int = 3, max_str: int = 80) -> Any:
    """Recursively redact a JSON-like structure for logging."""
    if depth > max_depth:
        return "<depth>"
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in list(obj.items())[:60]:
            if SENSITIVE_KEY.search(str(k)):
                out[k] = "<redacted>"
            else:
                out[k] = redact_record(v, depth + 1, max_depth, max_str)
        return out
    if isinstance(obj, list):
        head = [redact_record(v, depth + 1, max_depth, max_str) for v in obj[:5]]
        if len(obj) > 5:
            head.append(f"<+{len(obj) - 5} more>")
        return head
    if isinstance(obj, str):
        return truncate(redact_text(obj), max_str)
    return obj


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_PHONE = re.compile(r"(?<![\w,.])\+?\d{2,4}[\s-]?\d{3}[\s-]?\d{3,4}[\s-]?\d{0,4}(?![\w,.])")


def redact_text(text: str) -> str:
    """Blank obvious personal contact data inside free text (emails, phone numbers)."""
    text = _EMAIL.sub("<email>", text)
    return _PHONE.sub("<phone>", text)
