"""Fetching: rate-limited HTTP first, headless Chromium second, challenge detection.

The site is treated as untrusted input. Only header and cookie NAMES are ever kept for
logging; bodies are kept in memory for extraction and never written to disk.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import requests

from . import log
from .config import Settings
from .normalize import REF_RE, norm_text, visible_text
from .redact import safe_url
from .retry import retry


class TransientHTTPError(Exception):
    pass


class BrowserError(RuntimeError):
    """Any headless-browser failure. RuntimeError so callers degrade instead of crashing."""


class RequestBudgetExceeded(Exception):
    pass


@dataclass
class CapturedRequest:
    method: str
    url: str
    resource_type: str
    post_param_names: list[str] = field(default_factory=list)


@dataclass
class CapturedResponse:
    url: str
    status: int
    content_type: str
    body_len: int
    json: Any = None
    method: str = "GET"
    resource_type: str = ""


@dataclass
class FetchResult:
    url: str
    status: int
    text: str
    final_url: str
    content_type: str
    header_names: list[str]
    cookie_names: list[str]
    elapsed_s: float
    path: str  # 'requests' | 'browser'
    captured: list[CapturedResponse] = field(default_factory=list)
    requests_seen: list[CapturedRequest] = field(default_factory=list)
    body_text: Optional[str] = None  # browser innerText: respects CSS visibility, unlike stripped HTML

    @property
    def title(self) -> str:
        m = re.search(r"<title[^>]*>(.*?)</title>", self.text or "", re.I | re.S)
        return norm_text(re.sub(r"<[^>]+>", "", m.group(1))) or "" if m else ""

    @property
    def ref_count(self) -> int:
        return len(set(REF_RE.findall(self.text or "")))

    def page_text(self) -> str:
        """What a visitor sees: browser innerText when available, else markup-stripped HTML."""
        return self.body_text if self.body_text is not None else visible_text(self.text)


class RateLimiter:
    """>= min_interval seconds between requests to the source; hard cap per run."""

    def __init__(self, min_interval: float, max_requests: int):
        self.min_interval = min_interval
        self.max_requests = max_requests
        self.count = 0
        self._last = 0.0

    def wait(self) -> None:
        if self.count >= self.max_requests:
            raise RequestBudgetExceeded(f"request budget of {self.max_requests} per run exhausted")
        delta = time.monotonic() - self._last
        if self._last and delta < self.min_interval:
            time.sleep(self.min_interval - delta)
        self._last = time.monotonic()
        self.count += 1


class HttpFetcher:
    """Cookie-primed requests.Session: GET the human landing page once, then the query."""

    def __init__(self, settings: Settings, limiter: RateLimiter):
        self.settings = settings
        self.limiter = limiter
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": settings.source.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-GB,en;q=0.9",
            "Upgrade-Insecure-Requests": "1",
        })
        self._primed = False

    @property
    def landing(self) -> str:
        return self.settings.source.host.rstrip("/") + "/en"

    def prime(self) -> None:
        if self._primed:
            return
        self._primed = True
        try:
            self.limiter.wait()
            self.session.get(self.landing, timeout=self.settings.source.request_timeout_s)
        except requests.RequestException as e:
            log.warning(f"prime request failed: {type(e).__name__}")

    def get(self, url: str, referer: Optional[str] = None, prime: bool = True) -> FetchResult:
        if prime:
            self.prime()
        return self._get(url, referer or self.landing)

    @retry(attempts=3, base_delay=2.0,
           exceptions=(requests.ConnectionError, requests.Timeout, TransientHTTPError))
    def _get(self, url: str, referer: str) -> FetchResult:
        self.limiter.wait()
        t0 = time.monotonic()
        r = self.session.get(url, headers={"Referer": referer},
                             timeout=self.settings.source.request_timeout_s, allow_redirects=True)
        if 500 <= r.status_code < 600:
            raise TransientHTTPError(f"HTTP {r.status_code}")
        return FetchResult(
            url=url, status=r.status_code, text=r.text or "", final_url=r.url,
            content_type=r.headers.get("content-type", ""),
            header_names=sorted({k.lower() for k in r.headers.keys()}),
            cookie_names=sorted({c.name.lower() for c in self.session.cookies}),
            elapsed_s=time.monotonic() - t0, path="requests",
        )


class BrowserFetcher:
    """Headless Chromium via Playwright (lazy import). Captures XHR/fetch JSON bodies."""

    def __init__(self, settings: Settings, limiter: RateLimiter, capture_all_json: bool = False):
        self.settings = settings
        self.limiter = limiter
        self.capture_all_json = capture_all_json
        self._pw = None
        self._browser = None
        self._ctx = None
        self._page = None

    def __enter__(self) -> BrowserFetcher:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:  # pragma: no cover
            raise BrowserError("playwright is not installed in this environment") from e
        try:
            self._pw = sync_playwright().start()
            # full Chromium (new headless) rather than headless_shell: closer to a real visitor
            self._browser = self._pw.chromium.launch(
                headless=True, channel="chromium",
                args=["--disable-blink-features=AutomationControlled"])
            self._ctx = self._browser.new_context(
                user_agent=self.settings.source.user_agent, locale="en-GB", timezone_id="Asia/Dubai",
                viewport={"width": 1366, "height": 900},
                extra_http_headers={"Accept-Language": "en-GB,en;q=0.9"},
            )
            self._page = self._ctx.new_page()
        except Exception as e:  # __exit__ does not run when __enter__ raises: clean up here
            self.__exit__(None, None, None)
            raise BrowserError(f"browser launch failed: {type(e).__name__}: {str(e)[:160]}") from e
        return self

    def __exit__(self, *exc) -> None:
        for closer in (self._ctx, self._browser):
            try:
                if closer:
                    closer.close()
            except Exception:
                pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass

    def get(self, url: str, wait_selector: Optional[str] = None, counter_regex: Optional[str] = None,
            no_results_markers: Optional[list[str]] = None, timeout_s: int = 20) -> FetchResult:
        assert self._page is not None and self._ctx is not None
        page = self._page
        captured: list[CapturedResponse] = []
        seen: list[CapturedRequest] = []
        pattern = None

        def on_request(req):
            try:
                rt = req.resource_type
                if rt not in ("xhr", "fetch", "document"):
                    return
                names: list[str] = []
                data = req.post_data
                if data:
                    data = data.strip()
                    if data.startswith("{"):
                        try:
                            names = sorted(json.loads(data).keys())[:40]
                        except Exception:
                            names = ["<json>"]
                    else:
                        names = sorted({p.split("=", 1)[0] for p in data.split("&") if p})[:40]
                seen.append(CapturedRequest(method=req.method, url=req.url, resource_type=rt, post_param_names=names))
            except Exception:
                pass
        if self.settings.fetch.browser.api_url_pattern:
            pattern = re.compile(self.settings.fetch.browser.api_url_pattern)

        def on_response(resp):
            try:
                rt = resp.request.resource_type
                if rt not in ("xhr", "fetch"):
                    return
                ct = (resp.headers.get("content-type") or "").lower()
                if not (self.capture_all_json or (pattern and pattern.search(resp.url))):
                    return
                body = b""
                try:
                    body = resp.body()
                except Exception:
                    pass
                parsed = None
                if body and ("json" in ct or body[:1] in (b"{", b"[")):
                    try:
                        parsed = json.loads(body.decode("utf-8", "replace"))
                    except ValueError:
                        parsed = None
                captured.append(CapturedResponse(url=resp.url, status=resp.status, content_type=ct,
                                                 body_len=len(body), json=parsed,
                                                 method=resp.request.method, resource_type=rt))
            except Exception as e:  # never let a capture error break the fetch
                log.warning(f"response capture skipped: {type(e).__name__}")

        page.on("response", on_response)
        page.on("request", on_request)
        self.limiter.wait()
        t0 = time.monotonic()
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
            status = resp.status if resp else 0
            # The results grid is filled by JavaScript after load (and after the bot-manager
            # challenge completes), so: settle the network first, then wait for the results
            # counter or an explicit no-results message in the VISIBLE text.
            try:
                page.wait_for_load_state("networkidle", timeout=min(15, timeout_s) * 1000)
            except Exception:
                pass
            body = ""
            try:
                rx = re.compile(counter_regex, re.I) if counter_regex else None
                markers = [m.lower() for m in (no_results_markers or [])]
                deadline = time.monotonic() + timeout_s
                while time.monotonic() < deadline:
                    if wait_selector and page.query_selector(wait_selector):
                        break
                    body = page.inner_text("body")
                    low = body.lower()
                    if (rx and rx.search(body)) or (markers and any(m in low for m in markers)):
                        break
                    page.wait_for_timeout(500)
            except Exception as e:
                log.warning(f"browser wait ended early: {type(e).__name__}")
            page.wait_for_timeout(1000)
            text = page.content()
            try:
                body = page.inner_text("body")
            except Exception:
                body = body or visible_text(text)
            header_names = sorted({k.lower() for k in (resp.headers if resp else {}).keys()})
            cookie_names = sorted({c.get("name", "").lower() for c in self._ctx.cookies()})
            return FetchResult(url=url, status=status, text=text, final_url=page.url,
                               content_type=(resp.headers.get("content-type", "") if resp else ""),
                               header_names=header_names, cookie_names=cookie_names,
                               elapsed_s=time.monotonic() - t0, path="browser", captured=captured,
                               requests_seen=seen, body_text=body)
        except BrowserError:
            raise
        except Exception as e:
            raise BrowserError(f"browser fetch failed: {type(e).__name__}: {str(e)[:160]}") from e
        finally:
            for ev, fn in (("response", on_response), ("request", on_request)):
                try:
                    page.remove_listener(ev, fn)
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Challenge / WAF detection
# ---------------------------------------------------------------------------
CHALLENGE_TITLE = re.compile(
    r"access denied|attention required|just a moment|request unsuccessful|pardon our interruption|"
    r"incapsula|please verify|service unavailable|forbidden|blocked|captcha|are you a human|"
    r"error 4\d\d|error 5\d\d|maintenance", re.I)
CHALLENGE_BODY = re.compile(
    r"_Incapsula_Resource|cf-chl|cf_chl_opt|challenge-platform|Request unsuccessful\. Incapsula|"
    r"Radware Captcha Page|"
    r"Please enable JavaScript and cookies to continue|Access Denied.{0,80}permission to access|"
    r"The requested URL was rejected", re.I | re.S)
WAF_HEADER_NAMES = {"cf-ray", "cf-cache-status", "x-iinfo", "x-cdn", "x-amzn-waf-action", "x-amz-cf-id",
                    "x-akamai-transformed", "akamai-grn", "x-sucuri-id", "x-served-by"}
WAF_COOKIE_NAMES = {"incap_ses", "visid_incap", "bigipserver", "__cf_bm", "cf_clearance", "ak_bmsc",
                    "bm_sz", "_abck", "ts01", "awsalb", "awsalbcors", "reese84"}


def waf_fingerprint(r: FetchResult) -> list[str]:
    names: list[str] = []
    names += [h for h in r.header_names if h in WAF_HEADER_NAMES or h.startswith("x-iinfo")]
    names += [f"cookie:{c}" for c in r.cookie_names
              if any(c.startswith(w) for w in WAF_COOKIE_NAMES)]
    return sorted(set(names))


def looks_like_results_page(text: str, counter_regex: str, no_results_markers: list[str],
                            body_text: Optional[str] = None) -> bool:
    if not text:
        return False
    visible = body_text if body_text is not None else visible_text(text)
    if re.search(counter_regex, visible, re.I):
        return True
    if REF_RE.search(text):
        return True
    low = visible.lower()                     # markers are matched on visible text, never on scripts
    return any(m.lower() in low for m in no_results_markers)


def _offsite(final_url: str, requested_url: str) -> bool:
    try:
        from urllib.parse import urlsplit
        a, b = urlsplit(final_url).netloc.lower(), urlsplit(requested_url).netloc.lower()
        return bool(a) and bool(b) and a != b
    except Exception:
        return False


def detect_challenge(r: FetchResult, counter_regex: str, no_results_markers: list[str]) -> Optional[str]:
    """Return a reason string when the response is not a genuine results page."""
    if r.status != 200:
        return f"http_status_{r.status}"
    if _offsite(r.final_url, r.url):
        return f"redirected_offsite:{safe_url(r.final_url)[:80]}"
    title = r.title
    if CHALLENGE_TITLE.search(title):
        return f"challenge_title:{title[:60]}"
    if CHALLENGE_BODY.search(r.text or ""):
        return "challenge_body_marker"
    if not looks_like_results_page(r.text, counter_regex, no_results_markers, r.body_text):
        size = len(r.text or "")
        return f"not_a_results_page(bytes={size},title={title[:40]!r},url={safe_url(r.final_url)})"
    return None
