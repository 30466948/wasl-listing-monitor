from datetime import UTC

from waslmon.extract import extract_dom
from waslmon.filters import build_records
from waslmon.notify import RunInfo, alert_title, bed_tag, community_name, fmt_time, render_alert_body


def _match(page_html, settings, now):
    recs, _ = build_records(extract_dom(page_html, settings), now, settings)
    for r in recs:
        r.alert_kind = "new"
    return [r for r in recs if r.matched]


def test_titles(page_html, settings, now):
    recs = _match(page_html, settings, now)
    assert bed_tag(settings) == "2BR" and community_name(settings) == "wasl Village"
    assert alert_title("new", recs, settings) == "[wasl] NEW 2BR: R1083-35 Unit 101 - AED 47,000"
    assert alert_title("relisted", recs * 2, settings) == "[wasl] 2 RE-LISTED 2BR listings in wasl Village"
    assert alert_title("baseline", recs, settings).startswith("[wasl] BASELINE ESTABLISHED - 1 matching 2BR listing")
    assert "ANOMALY" in alert_title("anomaly", recs, settings)


def test_body_content_and_cap(page_html, settings, now):
    recs = _match(page_html, settings, now)
    info = RunInfo(run_url="https://github.com/o/r/actions/runs/1", breakdown="fetched=3 matched=1",
                   fetch_path="requests", query_url=settings.search_url(), total_records=3, gap_hours=4.5)
    body = render_alert_body("new", recs, settings, info)
    assert "AED 47,000" in body and "IM00100279345" in body and "open listing](https://www.wasl.ae/en/unit/residential/IM00100279345" in body
    assert "MISSED RUNS DETECTED" in body and "Asia/Dubai" in body and "workflow run" in body
    assert "rent <= AED 60,000" in body
    many = []
    for i in range(14):
        c = recs[0].model_copy(deep=True)
        c.ref = f"IM001000{i:05d}"
        many.append(c)
    body = render_alert_body("new", many, settings, info)
    assert body.count("open listing") == 10 and "and 4 more" in body


def test_unclassified_flag(page_html, settings, now):
    recs = _match(page_html, settings, now)
    recs[0].unclassified, recs[0].matched, recs[0].rent_aed = True, False, None
    body = render_alert_body("new", recs, settings, RunInfo())
    assert "check manually" in body


def test_fmt_time():
    from datetime import datetime
    s = fmt_time(datetime(2026, 9, 21, 10, 37, tzinfo=UTC), "Asia/Dubai")
    assert s == "2026-09-21 14:37 Asia/Dubai (10:37 UTC)"
    assert fmt_time(None, "Asia/Dubai") == "-"
