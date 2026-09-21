from waslmon.normalize import (
    content_hash,
    fingerprint,
    parse_aed,
    parse_bedrooms,
    parse_bedrooms_label,
    parse_building_code,
    parse_ref,
    parse_sqft,
    parse_unit_no,
)


def test_bedrooms_variants():
    assert parse_bedrooms("Studio") == 0
    assert parse_bedrooms("1 Bedroom") == 1
    assert parse_bedrooms("2 Bedroom Large") == 2
    assert parse_bedrooms("2 Bedrooms (extra large)") == 2
    assert parse_bedrooms("3 Bedroom + Store") == 3
    assert parse_bedrooms("2") == 2
    assert parse_bedrooms("") is None
    assert parse_bedrooms(None) is None
    assert parse_bedrooms("Large apartment") is None


def test_bedroom_label_from_card_text():
    assert parse_bedrooms_label("R1083-35 2 Bedroom Large 794 sq.ft. Unit #101 AED 47,000") == "2 Bedroom Large"
    assert parse_bedrooms_label("Studio 420 sq.ft.") == "Studio"
    assert parse_bedrooms_label("x 2 Bedrooms (extra large) y").startswith("2 Bedrooms")


def test_aed():
    assert parse_aed("AED 47,000") == 47000
    assert parse_aed("AED 185,999 Yearly") == 185999
    assert parse_aed("AED 45,000 - 50,000") == 45000
    assert parse_aed("47,000 /yr") == 47000
    assert parse_aed("AED") is None
    assert parse_aed(None) is None


def test_sqft():
    assert parse_sqft("2,112.00 sq.ft.") == 2112.0
    assert parse_sqft("1,084-1,190") == 1084.0
    assert parse_sqft("794") == 794.0
    assert parse_sqft("n/a") is None


def test_ref_building_unit():
    assert parse_ref("https://www.wasl.ae/en/unit/residential/IM00100279345") == "IM00100279345"
    assert parse_ref("IM001") is None
    assert parse_building_code("wasl Village - Building 35 R1083-35") == "R1083-35"
    assert parse_unit_no("Unit #101") == "101"
    assert parse_unit_no("Unit No: 305") == "305"
    assert parse_unit_no("Unit Type F1B") is None


def test_fingerprint_and_hash_stable():
    a = fingerprint("IM1", "R1083-35", None, "101", "F2B", 794.4)
    b = fingerprint("IM2", "r1083-35", "wasl Village", "101", "f2b", 794.9)
    assert a == b  # same physical unit under a new ref
    assert fingerprint("IM3", None, None, None, None, None) == "ref:IM3"
    assert content_hash(47000, "2 Bedroom", "794", "B35", "101") == content_hash(47000, "2 bedroom", "794", "b35", "101")
    assert content_hash(47000, "2 Bedroom", "794", "B35", "101") != content_hash(48000, "2 Bedroom", "794", "B35", "101")
