"""Parsers against the card format observed on wasl.ae in discovery (2026-09-22)."""
from pathlib import Path

from waslmon.extract import extract_dom
from waslmon.filters import build_records
from waslmon.normalize import (
    find_rent_text,
    find_size_text,
    find_type_text,
    parse_aed,
    parse_bedrooms,
    parse_sqft,
    parse_unit_no,
    split_heading,
)

FIX = Path(__file__).parent / "fixtures"
CARD = ("R441 - al barsha Al Barsha First Unit No. 604 Price \u00ea 101,999 / Year Size (Sq.ft.) 1,355.00 "
        "Type: 2 room flat Parking: PB255 Enquire")


def test_label_parsers_on_real_card_text():
    assert find_rent_text(CARD).startswith("Price") and parse_aed(find_rent_text(CARD)) == 101999
    assert find_size_text(CARD).startswith("Size") and parse_sqft(find_size_text(CARD)) == 1355.0
    assert find_type_text(CARD) == "2 room flat" and parse_bedrooms("2 room flat") == 2
    assert parse_bedrooms("3 bedroom") == 3 and parse_bedrooms("Studio") == 0
    assert parse_unit_no(CARD) == "604"
    assert split_heading("R441 - al barsha") == ("R441", "al barsha")
    assert split_heading("R1083-35 - wasl village") == ("R1083-35", "wasl village")
    assert split_heading("wasl Village - Building 35") == (None, None)


def test_extract_wasl_fixture(wasl_settings, now):
    html = (FIX / "search_page_wasl.html").read_text(encoding="utf-8")
    settings = wasl_settings
    raws = extract_dom(html, settings)
    assert [r.ref for r in raws] == ["IM00100279345", "IM00100280227"]
    a = raws[0]
    assert a.building == "R1083-35 - wasl village" and a.building_code == "R1083-35" and a.community == "wasl village"
    assert a.location == "Al Qusais Ind. Fifth" and a.unit_no == "101"
    assert parse_aed(a.rent_raw) == 47000 and a.bedrooms_raw == "2 room flat" and parse_sqft(a.size_raw) == 794.0
    recs, bd = build_records(raws, now, settings)
    by = {r.ref: r for r in recs}
    assert by["IM00100279345"].matched and by["IM00100279345"].rent_aed == 47000 and by["IM00100279345"].bedrooms == 2
    assert not by["IM00100280227"].matched and by["IM00100280227"].reject_reasons == ["wrong_bedrooms", "over_price"]
    assert by["IM00100279345"].fingerprint != f"ref:{by['IM00100279345'].ref}"
    assert bd.matched == 1


def test_hidden_no_results_div_does_not_mask_results(wasl_settings):
    settings = wasl_settings
    from waslmon.fetch import FetchResult, detect_challenge
    html = (FIX / "search_page_wasl.html").read_text(encoding="utf-8")
    url = "https://www.wasl.ae/en/search/residential?community=wasl-village"
    r = FetchResult(url=url, status=200, text=html, final_url=url, content_type="text/html", header_names=[],
                    cookie_names=[], elapsed_s=0.1, path="browser", body_text="1-2 of 2 records found R1083-35 ...")
    assert detect_challenge(r, settings.extract.counter_regex, settings.extract.no_results_markers) is None
    r2 = FetchResult(url=url, status=200, text=html, final_url="https://validate.perfdrive.com/abc/?ssa",
                     content_type="text/html", header_names=[], cookie_names=[], elapsed_s=0.1, path="requests")
    assert detect_challenge(r2, settings.extract.counter_regex, settings.extract.no_results_markers).startswith("redirected_offsite")
