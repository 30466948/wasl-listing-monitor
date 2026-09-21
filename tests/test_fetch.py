import pytest

from waslmon.fetch import (
    FetchResult,
    RateLimiter,
    RequestBudgetExceeded,
    detect_challenge,
    looks_like_results_page,
    waf_fingerprint,
)


def _res(text, status=200, headers=None, cookies=None, url="https://www.wasl.ae/en/search/residential?community=x"):
    return FetchResult(url=url, status=status, text=text, final_url=url, content_type="text/html",
                       header_names=headers or [], cookie_names=cookies or [], elapsed_s=0.1, path="requests")


def test_challenge_detection(page_html, challenge_html, settings):
    rx, markers = settings.extract.counter_regex, settings.extract.no_results_markers
    assert detect_challenge(_res(page_html), rx, markers) is None
    assert detect_challenge(_res(page_html, status=403), rx, markers) == "http_status_403"
    assert detect_challenge(_res(challenge_html), rx, markers).startswith("challenge_title")
    assert detect_challenge(_res("<html><title>Wasl</title><body>tiny</body></html>"), rx, markers).startswith("not_a_results_page")
    ok_empty = "<html><title>Search</title><body><p>No results found for your search</p></body></html>"
    assert detect_challenge(_res(ok_empty), rx, markers) is None
    assert looks_like_results_page(ok_empty, rx, markers)


def test_waf_fingerprint_names_only():
    r = _res("x", headers=["cf-ray", "content-type"], cookies=["incap_ses_123", "session"])
    assert waf_fingerprint(r) == ["cf-ray", "cookie:incap_ses_123"]


def test_title_and_refcount(page_html):
    r = _res(page_html)
    assert r.title.startswith("Residential 2 Bedrooms")
    assert r.ref_count == 3


def test_rate_limiter_budget():
    rl = RateLimiter(0.0, 2)
    rl.wait()
    rl.wait()
    with pytest.raises(RequestBudgetExceeded):
        rl.wait()
