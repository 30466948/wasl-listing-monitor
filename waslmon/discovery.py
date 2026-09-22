"""Discovery mode: probe the live site and print, redacted, everything the builder needs
to tailor config.yaml. Writes no state, sends no notifications, runs only on dispatch.

Output is organised in ::group:: sections and every line is <= 500 chars so it can be
read back through the GitHub job-logs API.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from . import log
from .config import Settings
from .extract import (
    _first,
    card_for,
    extract_dom,
    find_record_arrays,
    has_no_results_marker,
    key_paths,
    parse_counter,
    selector_chain,
    structure_signature,
    visible_text,
)
from .fetch import (
    BrowserFetcher,
    FetchResult,
    HttpFetcher,
    RateLimiter,
    RequestBudgetExceeded,
    detect_challenge,
    looks_like_results_page,
    waf_fingerprint,
)
from .filters import build_records
from .normalize import REF_RE
from .redact import param_names, redact_record, redact_text, safe_url, truncate

BUILDING_SLUGS = [5, 10, 13, 19, 34, 35, 38, 39, 57, 62]
# Drupal Views pagers are 0-indexed: ?page=1 is the SECOND page. Try that first.
PAGINATION_CANDIDATES = [("page", "1"), ("page", "2"), ("p", "2"), ("pageNumber", "2"), ("page_no", "2"),
                         ("offset", "12"), ("start", "12")]
COUNT_WORDS_RE = re.compile(r"(\d[\d,]*)\s*(results?|records?|properties|units|listings|found)", re.I)


def _lines(text: str, width: int = 480) -> None:
    for i in range(0, len(text), width):
        log.info(text[i:i + width])


def _refs(text: str) -> set[str]:
    return set(REF_RE.findall(text or ""))


def robots(http: HttpFetcher, settings: Settings) -> Optional[bool]:
    host = settings.source.host.rstrip("/")
    try:
        r = http.get(f"{host}/robots.txt", prime=False)
    except Exception as e:  # noqa: BLE001
        log.warning(f"robots.txt fetch failed: {type(e).__name__}")
        return None
    log.info(f"status={r.status} bytes={len(r.text)} content-type={r.content_type[:40]}")
    if r.status != 200:
        log.info("verdict: no robots.txt served (treated as allow-all)")
        return True
    body = r.text[:20000]
    for line in body.splitlines()[:80]:
        log.info("  " + truncate(line, 200))
    # naive evaluation for User-agent: * (and any group naming us)
    ua_block = False
    disallows: list[str] = []
    for raw in body.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        k, _, v = line.partition(":")
        k, v = k.strip().lower(), v.strip()
        if k == "user-agent":
            ua_block = v == "*"
        elif k == "disallow" and ua_block and v:
            disallows.append(v)
    blocked = [d for d in disallows if any(p.startswith(d.rstrip("*")) for p in ("/en/search", "/en/unit", "/"))]
    if blocked:
        log.error(f"verdict: robots.txt DISALLOWS relevant paths for *: {blocked} -> STOP and report to the user")
        return False
    log.info(f"verdict: allowed (disallow rules for *: {disallows[:10] or 'none'})")
    return True


def sitemap(http: HttpFetcher, settings: Settings) -> None:
    host = settings.source.host.rstrip("/")
    for path in ("/sitemap.xml", "/sitemap_index.xml", "/en/sitemap.xml", "/sitemap-en.xml"):
        try:
            r = http.get(f"{host}{path}", prime=False)
        except Exception as e:  # noqa: BLE001
            log.info(f"{path}: fetch failed {type(e).__name__}")
            continue
        locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", r.text or "", re.I)
        lastmod = bool(re.search(r"<lastmod>", r.text or "", re.I))
        unit_locs = [u for u in locs if "/unit/" in u]
        log.info(f"{path}: status={r.status} bytes={len(r.text)} locs={len(locs)} unit_locs={len(unit_locs)} lastmod={lastmod}")
        for u in locs[:20]:
            log.info("  " + urlsplit(u).path)
        if unit_locs and lastmod:
            log.notice(f"{path} lists unit pages WITH lastmod: a sitemap-based detector is possible")


def probe_http(http: HttpFetcher, url: str, settings: Settings, label: str) -> Optional[FetchResult]:
    try:
        r = http.get(url)
    except Exception as e:  # noqa: BLE001
        log.error(f"{label}: fetch failed {type(e).__name__}: {truncate(str(e), 150)}")
        return None
    describe(r, settings, label)
    return r


def describe(r: FetchResult, settings: Settings, label: str) -> None:
    text = r.page_text()
    counter = parse_counter(text, settings.extract.counter_regex)
    reason = detect_challenge(r, settings.extract.counter_regex, settings.extract.no_results_markers)
    log.info(f"{label}: status={r.status} final={safe_url(r.final_url)} ct={r.content_type[:40]} bytes={len(r.text)} "
             f"elapsed={r.elapsed_s:.1f}s path={r.path}")
    log.info(f"{label}: title={r.title[:120]!r}")
    log.info(f"{label}: counter={counter} unique_refs={len(_refs(r.text))} unit_anchors={r.text.count('/unit/residential/')} "
             f"no_results_marker={has_no_results_marker(text, settings.extract.no_results_markers)}")
    log.info(f"{label}: header_names={r.header_names[:40]}")
    log.info(f"{label}: cookie_names={r.cookie_names[:20]} waf_fingerprint={waf_fingerprint(r)}")
    log.info(f"{label}: challenge_verdict={reason or 'none'} looks_like_results="
             f"{looks_like_results_page(r.text, settings.extract.counter_regex, settings.extract.no_results_markers)}")
    m = re.search(settings.extract.counter_regex, text, re.I)
    if m:
        s = max(0, m.start() - 80)
        log.info(f"{label}: counter context: ...{redact_text(text[s:m.end() + 80])}...")
    else:
        hits = [redact_text(text[max(0, x.start() - 40):x.end() + 40]) for x in COUNT_WORDS_RE.finditer(text)]
        log.info(f"{label}: count-like phrases in visible text: {hits[:8]}")
    log.info(f"{label}: visible_text_len={len(text)} first_300={redact_text(text[:300])!r}")


def network_report(r: FetchResult) -> None:
    log.info(f"requests seen (xhr/fetch/document): {len(r.requests_seen)}")
    for q in r.requests_seen[:60]:
        parts = urlsplit(q.url)
        log.info(f"  REQ {q.method} {q.resource_type} {parts.netloc}{parts.path} params={param_names(q.url)}"
                 + (f" post_params={q.post_param_names}" if q.post_param_names else ""))
    log.info(f"captured xhr/fetch responses: {len(r.captured)}")
    for c in r.captured[:40]:
        parts = urlsplit(c.url)
        log.info(f"{c.method} {parts.netloc}{parts.path} params={param_names(c.url)} status={c.status} "
                 f"ct={c.content_type[:30]} bytes={c.body_len} json={'yes' if c.json is not None else 'no'}")
        if c.json is None:
            continue
        if isinstance(c.json, dict):
            log.info(f"   top-level keys: {list(c.json.keys())[:40]}")
        else:
            log.info(f"   top-level type: {type(c.json).__name__} len={len(c.json) if hasattr(c.json, '__len__') else '?'}")
        for path, arr in find_record_arrays(c.json)[:3]:
            sample = redact_record(arr[0])
            log.info(f"   array path={path} len={len(arr)} record_keys={key_paths(arr[0])[:40]}")
            _lines("   sample=" + json.dumps(sample, ensure_ascii=False, default=str), 480)


def dom_report(html: str, settings: Settings, body_text: Optional[str] = None) -> None:
    soup = BeautifulSoup(html or "", "html.parser")
    text = body_text if body_text is not None else visible_text(html)
    log.info(f"title={soup.title.get_text(strip=True)[:120] if soup.title else ''!r} bytes={len(html or '')}")
    m = re.search(settings.extract.counter_regex, text, re.I)
    log.info(f"counter_text={m.group(0) if m else None}")
    anchors = soup.select('a[href*="/unit/residential/"]')
    hrefs = []
    for a in anchors:
        h = a.get("href") or ""
        if h not in hrefs:
            hrefs.append(h)
    log.info(f"unit anchors={len(anchors)} unique_hrefs={len(hrefs)}")
    for h in hrefs[:15]:
        log.info("  " + urlsplit(h).path)
    for a in anchors[:3]:
        log.info(f"anchor chain: {selector_chain(a, 6)}")
        card = card_for(a)
        log.info(f"card chain:   {selector_chain(card, 3)} text={truncate(redact_text(card.get_text(' ', strip=True)), 300)!r}")
    # Structure only: never print card markup (it can carry agent names / phone numbers).
    seen_cards = set()
    shown = 0
    for a in anchors:
        card = card_for(a)
        key = id(card)
        if key in seen_cards:
            continue
        seen_cards.add(key)
        log.info(f"--- card {shown + 1} structure (tag/class/attr names only) ---")
        log.info(f"  chain={selector_chain(card, 3)} tag={card.name} attr_names={sorted(card.attrs)[:20]}")
        for el in card.find_all(True)[:40]:
            href = urlsplit(el.get("href") or "").path if el.name == "a" else ""
            txt = el.get_text(" ", strip=True)
            low = txt.lower()
            kind = ("aed" if "aed" in low else "sqft" if "sq" in low and "ft" in low
                    else "bed" if "bed" in low or "studio" in low else "unit" if "unit" in low
                    else "building" if "building" in low else "")
            log.info(f"    {el.name} class={(el.get('class') or [])[:4]} attrs={sorted(el.attrs)[:10]}"
                     + (f" href_path={href}" if href else "") + (f" looks_like={kind}" if kind else "")
                     + f" text_len={len(txt)}")
        probes = {k: _first(card, v) for k, v in settings.extract.dom.fields.items() if v}
        if probes:
            log.info("  field probes: " + truncate(json.dumps(redact_record(probes), ensure_ascii=False), 400))
        shown += 1
        if shown >= 2:
            break
    low = text.lower()
    log.info(f"no_results markers present in VISIBLE text: {[m for m in settings.extract.no_results_markers if m in low]}")
    for m in settings.extract.no_results_markers:
        i = low.find(m)
        if i >= 0:
            log.info(f"  marker context: ...{redact_text(text[max(0, i - 80):i + 80])}...")
    # card texts as a visitor sees them (parser fixture material; listing data only)
    cards = soup.select(settings.extract.dom.card_selector or "section.all-units-cards") or [card_for(a) for a in anchors[:5]]
    for i, c in enumerate(cards[:6]):
        log.info(f"card_text[{i}]: {truncate(redact_text(c.get_text(' | ', strip=True)), 400)!r}")
    pager = soup.select('a[href*="page="], .pager a, nav[aria-label*="agination"] a, .pagination a')
    log.info(f"pager links: {len(pager)} first={[urlsplit(a.get('href') or '').query[:40] for a in pager[:8]]}")
    pager_text = [redact_text(el.get_text(' ', strip=True))[:120] for el in soup.select('.pager, .pagination, nav[aria-label*="agination"]')]
    log.info(f"pager text: {pager_text[:3]}")
    cands = []
    rx = re.compile(settings.extract.counter_regex, re.I)
    for el in soup.find_all(string=rx):
        parent = el.parent
        if parent is not None:
            cands.append(selector_chain(parent, 4))
    log.info(f"counter selector candidates: {cands[:5]}")
    facet = [redact_text(s.strip()) for s in soup.find_all(string=re.compile(r"Apartment\s*\(\d", re.I))]
    log.info(f"facet block strings: {facet[:5]}")
    log.info(f"structure signature: {json.dumps(structure_signature(html), default=str)[:400]}")


def pagination_probe(fetch: Callable[[str], FetchResult], settings: Settings, base_refs: set[str], total: Optional[int]) -> Optional[str]:
    if total is not None and total <= settings.extract.page_size:
        log.info(f"query total {total} fits on one page; pagination probe uses the control query instead")
    found = None
    for name, value in PAGINATION_CANDIDATES:
        params = dict(settings.source.control_params if (total is None or total <= settings.extract.page_size) else settings.source.params)
        params[name] = value
        url = settings.url_with_params(params)
        try:
            r = fetch(url)
        except RequestBudgetExceeded:
            log.warning("request budget exhausted during pagination probe")
            break
        except Exception as e:  # noqa: BLE001
            log.info(f"{name}={value}: fetch failed {type(e).__name__}")
            continue
        text = r.page_text()
        counter = parse_counter(text, settings.extract.counter_regex)
        refs = _refs(r.text)
        differs = bool(refs) and refs != base_refs
        lo_moved = bool(counter and counter[0] != 1)
        chal = detect_challenge(r, settings.extract.counter_regex, settings.extract.no_results_markers)
        log.info(f"{name}={value}: status={r.status} title={r.title[:50]!r} challenge={chal or 'none'} counter={counter} "
                 f"refs={len(refs)} differs_from_p1={differs} lo_moved={lo_moved} first_refs={sorted(refs)[:3]}")
        if lo_moved or (differs and counter is not None):
            found = name
            log.notice(f"pagination parameter appears to be '{name}' (mode={'offset' if value == '12' else 'page'})")
            break
    if not found:
        log.warning("no pagination parameter candidate worked; inspect BROWSER_NETWORK for the API's paging fields")
    return found


def coverage_probe(fetch: Callable[[str], FetchResult], settings: Settings, community_refs: set[str]) -> None:
    union: set[str] = set()
    for n in BUILDING_SLUGS:
        params = {"building": f"wasl-village-building-{n}", "room": settings.source.params.get("room", "2"),
                  "usage": settings.source.params.get("usage", "APARTMENT")}
        try:
            r = fetch(settings.url_with_params(params))
        except RequestBudgetExceeded:
            log.warning("request budget exhausted during coverage probe")
            break
        except Exception as e:  # noqa: BLE001
            log.info(f"building {n}: fetch failed {type(e).__name__}")
            continue
        refs = _refs(r.text)
        counter = parse_counter(r.page_text(), settings.extract.counter_regex)
        chal = detect_challenge(r, settings.extract.counter_regex, settings.extract.no_results_markers)
        union |= refs
        log.info(f"building {n}: status={r.status} title={r.title[:40]!r} challenge={chal or 'none'} counter={counter} "
                 f"refs={len(refs)} subset_of_community={refs <= community_refs}")
    missing = union - community_refs
    log.info(f"union of building queries={len(union)} community query p1={len(community_refs)} missing_from_community={sorted(missing)[:20]}")
    if missing:
        log.warning("community query misses refs that building queries return (or community has >1 page); check pagination")


def preview(html: str, settings: Settings) -> None:
    from datetime import datetime

    from .runner import table
    raws = extract_dom(html, settings)
    recs, bd = build_records(raws, datetime.now(UTC), settings)
    for line in table(recs):
        log.info(line)
    log.info(bd.summary())
    for raw in raws[:3]:
        log.info("raw sample: " + truncate(json.dumps(redact_record(raw.model_dump()), ensure_ascii=False), 480))


def run(settings: Settings, root: Path) -> int:
    limiter = RateLimiter(settings.source.min_seconds_between_requests, max(60, settings.source.max_requests_per_run))
    http = HttpFetcher(settings, limiter)
    log.info(f"query url: {settings.search_url()}")
    log.info(f"control url: {settings.control_url()}")
    with log.group("ROBOTS"):
        allowed = robots(http, settings)
    with log.group("SITEMAP"):
        sitemap(http, settings)
    if allowed is False:
        log.error("robots.txt disallows the search path; discovery stops here (Gate 1).")
        return 2
    with log.group("HTTP_SEARCH"):
        r1 = probe_http(http, settings.search_url(1), settings, "http_search")
    with log.group("HTTP_CONTROL"):
        rc = probe_http(http, settings.control_url(), settings, "http_control")
    http_ok = bool(r1) and detect_challenge(r1, settings.extract.counter_regex, settings.extract.no_results_markers) is None \
        and bool(_refs(r1.text))
    log.notice(f"http path usable for extraction: {http_ok}")
    base_html = r1.text if (r1 and http_ok) else ""
    base_refs = _refs(base_html)
    total = None
    if r1:
        c = parse_counter(visible_text(r1.text), settings.extract.counter_regex)
        total = c[2] if c else None
    fetch: Callable[[str], FetchResult] = lambda u: http.get(u)  # noqa: E731
    browser_html = ""
    try:
        with BrowserFetcher(settings, limiter, capture_all_json=True) as bf:
            b = settings.fetch.browser
            with log.group("BROWSER_NETWORK"):
                rb = bf.get(settings.search_url(1), b.wait_selector, settings.extract.counter_regex,
                            settings.extract.no_results_markers, b.wait_timeout_s)
                describe(rb, settings, "browser_search")
                network_report(rb)
            with log.group("BROWSER_DOM"):
                dom_report(rb.text, settings, rb.body_text)
            browser_html = rb.text
            if not http_ok:
                base_html, base_refs = rb.text, _refs(rb.text)
                c = parse_counter(rb.page_text(), settings.extract.counter_regex)
                total = c[2] if c else total
                fetch = lambda u: bf.get(u, b.wait_selector, settings.extract.counter_regex,  # noqa: E731
                                         settings.extract.no_results_markers, min(8, b.wait_timeout_s))
            with log.group("PAGINATION_PROBE"):
                pagination_probe(fetch, settings, base_refs, total)
            with log.group("COVERAGE_PROBE"):
                coverage_probe(fetch, settings, base_refs)
    except Exception as e:  # noqa: BLE001 - the browser path is optional in discovery
        log.warning(f"browser path unavailable ({type(e).__name__}: {str(e)[:200]}); probes continue on the http path only")
        if http_ok:
            with log.group("PAGINATION_PROBE"):
                pagination_probe(fetch, settings, base_refs, total)
            with log.group("COVERAGE_PROBE"):
                coverage_probe(fetch, settings, base_refs)
    with log.group("PARSED_PREVIEW"):
        preview(browser_html or base_html, settings)
    log.info(f"requests used this run: {limiter.count}")
    _ = rc
    return 0
