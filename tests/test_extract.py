import pytest

from waslmon.extract import extract_api, extract_dom, parse_counter, reconcile, structure_signature, visible_text
from waslmon.fetch import CapturedResponse
from waslmon.models import Degraded, RawListing


def test_counter():
    assert parse_counter("1-12 of 905 records found", r"(\d[\d,]*)\s*-\s*(\d[\d,]*)\s+of\s+(\d[\d,]*)\s+records?") == (1, 12, 905)
    assert parse_counter("1 - 3 of 3 records found", r"(\d[\d,]*)\s*-\s*(\d[\d,]*)\s+of\s+(\d[\d,]*)\s+records?") == (1, 3, 3)
    assert parse_counter("13-24 of 1,205 records", r"(\d[\d,]*)\s*-\s*(\d[\d,]*)\s+of\s+(\d[\d,]*)\s+records?") == (13, 24, 1205)
    assert parse_counter("no counter here", r"(\d+)-(\d+) of (\d+) records?") is None


def test_dom_generic_extraction(page_html, settings):
    recs = extract_dom(page_html, settings)
    assert [r.ref for r in recs] == ["IM00100279345", "IM00100280227", "IM00100276220"]
    r = recs[0]
    assert r.rent_raw.startswith("AED 47,000")
    assert r.bedrooms_raw == "2 Bedroom Large"
    assert r.size_raw.startswith("794")
    assert r.building_code == "R1083-35"
    assert r.unit_no == "101"
    assert "Building 35" in r.building
    assert r.url.endswith("/en/unit/residential/IM00100279345")
    assert recs[2].bedrooms_raw == "Studio"
    assert recs[1].size_raw.startswith("1,084-1,190")


def test_dom_with_selectors(page_html, settings):
    settings.extract.dom.card_selector = "div.property-card"
    settings.extract.dom.fields = {"building": "h3.title", "location": "p.location", "rent_raw": "div.price"}
    recs = extract_dom(page_html, settings)
    assert len(recs) == 3
    assert recs[0].location == "Al Qusais Ind. Fifth"
    assert recs[0].building == "wasl Village - Building 35"


def test_visible_text_strips_scripts():
    assert "alert" not in visible_text("<script>alert(1)</script><p>1-3 of 3 records found</p>")


def test_api_extraction(api_json, settings):
    settings.extract.api.records_path = "data.items"
    settings.extract.api.total_path = "data.total"
    settings.extract.api.field_map = {"ref": "unitRef", "building": "buildingText", "building_code": "buildingName",
                                      "unit_no": "unitNo", "unit_type": "unitType", "bedrooms_raw": "bedrooms",
                                      "rent_raw": "price", "size_raw": "size", "location": "locationText"}
    cap = [CapturedResponse(url="https://www.wasl.ae/api/x", status=200, content_type="application/json",
                            body_len=10, json=api_json)]
    out = extract_api(cap, settings)
    assert out is not None
    recs, total = out
    assert total == 2 and [r.ref for r in recs] == ["IM00100279345", "IM00100280227"]
    assert recs[0].extractor == "api" and recs[0].unit_type == "F2B"


def test_api_extraction_disabled_without_config(settings):
    assert extract_api([CapturedResponse("u", 200, "application/json", 1, {"a": 1})], settings) is None


def test_reconcile_agreement_and_disagreement(settings):
    dom = [RawListing(ref="IM00100000001", rent_raw="AED 1"), RawListing(ref="IM00100000002", rent_raw="AED 2")]
    api = [RawListing(ref="IM00100000001", unit_type="F2B", extractor="api"),
           RawListing(ref="IM00100000002", extractor="api")]
    merged, issues = reconcile(api, dom, settings)
    assert not issues and len(merged) == 2
    assert merged[0].unit_type == "F2B" and merged[0].rent_raw == "AED 1" and merged[0].extractor == "api"
    api_bad = [RawListing(ref="IM00100000009", extractor="api")]
    with pytest.raises(Degraded):
        reconcile(api_bad, dom, settings)
    settings.extract.require_agreement = False
    merged, issues = reconcile(api_bad, dom, settings)
    assert issues and len(merged) == 3


def test_structure_signature(page_html):
    sig = structure_signature(page_html, {"data": {"items": []}})
    assert sig["dom_anchor_count"] == 4
    assert "property-card" in sig["dom_card_chain"]
    assert "data.items" in sig["api_key_paths"]
