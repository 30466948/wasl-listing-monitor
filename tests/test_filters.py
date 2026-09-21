from waslmon.extract import extract_dom
from waslmon.filters import build_records, classify


def test_build_and_classify_scope_b(page_html, settings, now):
    recs, bd = build_records(extract_dom(page_html, settings), now, settings)
    by = {r.ref: r for r in recs}
    assert by["IM00100279345"].matched                       # 2BR, 47k
    assert not by["IM00100280227"].matched and by["IM00100280227"].reject_reasons == ["over_price"]
    assert not by["IM00100276220"].matched and "wrong_bedrooms" in by["IM00100276220"].reject_reasons
    assert bd.fetched == 3 and bd.matched == 1 and bd.rejected == {"over_price": 1, "wrong_bedrooms": 1}
    assert "fetched=3" in bd.summary()
    assert by["IM00100279345"].rent_aed == 47000 and by["IM00100279345"].bedrooms == 2
    assert by["IM00100279345"].size_sqft == 794.0
    assert by["IM00100279345"].price_history[0].rent_aed == 47000


def test_scope_a_and_c(page_html, settings, now):
    settings.filter.max_rent_aed = None                       # scope A
    recs, bd = build_records(extract_dom(page_html, settings), now, settings)
    assert bd.matched == 2
    settings.filter.community_contains = None                 # scope C-ish
    settings.filter.max_rent_aed = 60000
    recs, bd = build_records(extract_dom(page_html, settings), now, settings)
    assert bd.matched == 1


def test_unclassified_when_fields_missing(page_html, settings, now):
    recs, _ = build_records(extract_dom(page_html, settings), now, settings)
    r = recs[0]
    r.rent_aed = None
    matched, unclassified, reasons = classify(r, settings.filter)
    assert not matched and unclassified and reasons == []
    settings.filter.include_unclassified = False
    matched, unclassified, reasons = classify(r, settings.filter)
    assert not matched and not unclassified and reasons == ["unparsed_fields"]


def test_wrong_community_rejected(page_html, settings, now):
    recs, _ = build_records(extract_dom(page_html, settings), now, settings)
    r = recs[0]
    r.building, r.building_code, r.community, r.location = "wasl Oasis - Building 2", "R500-2", None, "Muhaisnah"
    matched, _, reasons = classify(r, settings.filter)
    assert not matched and reasons == ["wrong_community"]
