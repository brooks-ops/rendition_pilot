from __future__ import annotations

from app.comptroller.address_normalizer import normalize_address


def test_preserves_raw_value():
    n = normalize_address("123 Main St Ste 200")
    assert n.raw == "123 Main St Ste 200"


def test_street_suffix_abbreviations_normalize_the_same():
    variants = ["123 Main St", "123 Main Street", "123 MAIN ST."]
    normalized = {normalize_address(v).normalized for v in variants}
    assert len(normalized) == 1


def test_directional_prefix_normalizes():
    n = normalize_address("1234 N Loop 289")
    assert "NORTH" in n.normalized
    assert n.base_address == "1234 NORTH LOOP 289"


def test_highway_and_farm_to_market_abbreviations():
    assert "HIGHWAY" in normalize_address("100 US Hwy 84").normalized
    assert "FARM TO MARKET" in normalize_address("2200 FM 1585").normalized


def test_zip_plus_four_split():
    n = normalize_address("100 W University Ave, Lubbock, TX 79409-1234")
    assert n.zip5 == "79409"
    assert n.zip4 == "1234"


def test_zip_from_separate_column_wins_over_embedded():
    n = normalize_address("100 Main St 79401", zip_code="79999")
    assert n.zip5 == "79999"


def test_zip_without_plus_four():
    n = normalize_address("100 Main St", zip_code="79401")
    assert n.zip5 == "79401"
    assert n.zip4 is None


def test_suite_extraction_ste():
    n = normalize_address("123 Main St Ste 200")
    assert n.unit == "200"
    assert n.base_address == "123 MAIN STREET"
    assert n.has_unit is True


def test_suite_extraction_suite_word():
    n = normalize_address("123 Main St Suite 200")
    assert n.unit == "200"


def test_suite_extraction_hash_marker():
    n = normalize_address("123 Main St #200")
    assert n.unit == "200"


def test_apartment_marker_treated_as_unit():
    n = normalize_address("1234 N Loop 289 Apt 5")
    assert n.unit == "5"
    assert n.base_address == "1234 NORTH LOOP 289"


def test_no_unit_when_none_present():
    n = normalize_address("123 Main St")
    assert n.unit is None
    assert n.has_unit is False


def test_different_suites_do_not_collapse_to_the_same_base_and_unit():
    a = normalize_address("123 Main St Ste 100")
    b = normalize_address("123 Main St Ste 200")
    assert a.base_address == b.base_address
    assert a.unit != b.unit


def test_punctuation_and_whitespace_normalized():
    n = normalize_address("  123   Main   St.  ")
    assert n.normalized == "123 MAIN STREET"


def test_trailing_city_after_comma_is_stripped():
    """Real CAD situs exports commonly give 'STREET, CITY[, STATE]' as one
    field (e.g. Lubbock's SitusAddress column) rather than a clean
    street-only line -- found importing the real 234k-row Lubbock export,
    where this caused an exact property match to score as no match at all
    (the property's own city name was being compared as if it were street
    text)."""

    assert normalize_address("5807 88TH PL, LUBBOCK, TX").normalized == "5807 88TH PLACE"
    assert normalize_address("5501 ACUFF RD, LUBBOCK").normalized == "5501 ACUFF ROAD"
    assert normalize_address("5807 88TH PL, LUBBOCK, TX  79424").zip5 == "79424"


def test_capitalization_normalized():
    a = normalize_address("123 main street")
    b = normalize_address("123 MAIN STREET")
    assert a.normalized == b.normalized


def test_empty_and_none_address_do_not_crash():
    assert normalize_address(None).normalized == ""
    assert normalize_address("").normalized == ""
    assert normalize_address("   ").normalized == ""
