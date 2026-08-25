from __future__ import annotations

from app.comptroller.address_normalizer import normalize_address, normalize_mailing_address


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


# -- normalize_mailing_address / PO Box handling ------------------------------

def test_po_box_variants_normalize_to_the_same_form():
    variants = ["PO Box 500", "P.O. Box 500", "P O BOX 500", "po box 500"]
    normalized = {normalize_mailing_address(v).normalized_line for v in variants}
    assert normalized == {"PO BOX 500"}


def test_po_box_address_type_is_po_box():
    n = normalize_mailing_address("PO Box 500")
    assert n.address_type == "PO_BOX"
    assert n.po_box_number == "500"


def test_street_address_type_is_street():
    n = normalize_mailing_address("123 Main St")
    assert n.address_type == "STREET"
    assert n.po_box_number is None


def test_mailing_address_preserves_city_state_zip_as_separate_fields():
    n = normalize_mailing_address("123 Main St", city="Lubbock", state="tx", zip_code="79401")
    assert n.city == "LUBBOCK"
    assert n.state == "TX"
    assert n.zip5 == "79401"
    assert n.full_normalized == "123 MAIN STREET LUBBOCK TX 79401"


def test_mailing_address_does_not_drop_city_via_comma_splitting():
    """Unlike normalize_address() (situs), a mailing address's city/state
    passed as separate fields must survive -- this is the exact behavior
    that would break if normalize_mailing_address ever delegated straight
    to normalize_address()."""

    n = normalize_mailing_address("PO Box 456", city="Lubbock", state="TX", zip_code="79408")
    assert n.city == "LUBBOCK"
    assert "LUBBOCK" in n.full_normalized


def test_mailing_address_suite_extracted_like_situs():
    n = normalize_mailing_address("7610 Milwaukee Ave Ste 300")
    assert n.unit == "300"
    assert n.normalized_line == "7610 MILWAUKEE AVENUE"


def test_mailing_address_blank_line_is_unknown_type():
    n = normalize_mailing_address(None)
    assert n.address_type == "UNKNOWN"
    assert n.normalized_line == ""


def test_mailing_address_blank_line_still_keeps_city_if_given():
    n = normalize_mailing_address(None, city="Lubbock", state="TX")
    assert n.city == "LUBBOCK"
    assert n.normalized_line == ""
