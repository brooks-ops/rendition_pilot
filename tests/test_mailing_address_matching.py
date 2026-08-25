"""Tests for app/comptroller/mailing_address_matching.py -- the pure
address-delta comparison engine. Covers every special case explicitly
listed in the Mailing Address Intelligence spec (item 30) plus the two
real bugs found while building this: full_normalized ignoring unit (which
made a suite addition look identical to no-suite), and the generic _diff
helper conflating "no suite" with "unknown data" for the unit component.
"""

from __future__ import annotations

from app.comptroller.mailing_address_matching import (
    FORMAT_ONLY_DIFFERENCE,
    INSUFFICIENT_DATA,
    LIKELY_CHANGE,
    POSSIBLE_CHANGE,
    SAME_ADDRESS,
    compare_mailing_addresses,
)


def compare(cr, cc, cs, cz, orw, oc, ost, oz):
    return compare_mailing_addresses(
        current_raw=cr, current_city=cc, current_state=cs, current_zip=cz,
        observed_raw=orw, observed_city=oc, observed_state=ost, observed_zip=oz,
    )


def test_exact_same_address():
    r = compare("123 Main St", "Lubbock", "TX", "79401", "123 Main St", "Lubbock", "TX", "79401")
    assert r.classification == SAME_ADDRESS


def test_street_suffix_normalization_is_not_a_change():
    r = compare("123 Main St", "Lubbock", "TX", "79401", "123 Main Street", "Lubbock", "TX", "79401")
    assert r.classification == SAME_ADDRESS


def test_punctuation_only_difference_is_not_a_change():
    r = compare("123 Main St.", "Lubbock", "TX", "79401", "123 Main St", "Lubbock", "TX", "79401")
    assert r.classification == SAME_ADDRESS


def test_capitalization_only_difference_is_not_a_change():
    r = compare("123 main st", "lubbock", "tx", "79401", "123 MAIN ST", "LUBBOCK", "TX", "79401")
    assert r.classification == SAME_ADDRESS


def test_zip_plus_four_only_is_not_a_change():
    r = compare("123 Main St", "Lubbock", "TX", "79401", "123 Main St", "Lubbock", "TX", "79401-1234")
    assert r.classification == SAME_ADDRESS


def test_po_box_formatting_variants_are_not_a_change():
    r = compare("PO Box 500", "Lubbock", "TX", "79401", "P.O. Box 500", "Lubbock", "TX", "79401")
    assert r.classification == SAME_ADDRESS


def test_po_box_number_change_is_likely_change():
    r = compare("PO Box 100", "Lubbock", "TX", "79401", "PO Box 900", "Lubbock", "TX", "79401")
    assert r.classification == LIKELY_CHANGE
    assert r.change_confidence == "HIGH"


def test_street_address_change_is_likely_change():
    r = compare("123 Main St", "Lubbock", "TX", "79401", "456 Oak Ave", "Lubbock", "TX", "79401")
    assert r.classification == LIKELY_CHANGE


def test_street_to_po_box_is_likely_change():
    r = compare("123 Main St", "Lubbock", "TX", "79401", "PO Box 100", "Lubbock", "TX", "79401")
    assert r.classification == LIKELY_CHANGE
    assert r.differences["address_type"] == "CHANGED"


def test_po_box_to_street_is_likely_change():
    r = compare("PO Box 100", "Lubbock", "TX", "79401", "123 Main St", "Lubbock", "TX", "79401")
    assert r.classification == LIKELY_CHANGE
    assert r.differences["address_type"] == "CHANGED"


def test_suite_added_is_possible_change_not_likely():
    r = compare("123 Main St", "Lubbock", "TX", "79401", "123 Main St Ste 200", "Lubbock", "TX", "79401")
    assert r.classification == POSSIBLE_CHANGE
    assert r.differences["unit"] == "CHANGED"


def test_suite_removed_is_possible_change():
    r = compare("123 Main St Ste 200", "Lubbock", "TX", "79401", "123 Main St", "Lubbock", "TX", "79401")
    assert r.classification == POSSIBLE_CHANGE


def test_suite_changed_between_two_real_suites_is_likely_change():
    r = compare("123 Main St Ste 100", "Lubbock", "TX", "79401", "123 Main St Ste 500", "Lubbock", "TX", "79401")
    assert r.classification == LIKELY_CHANGE
    assert r.change_confidence == "MEDIUM"


def test_city_normalization_case_only_is_not_a_change():
    r = compare("123 Main St", "lubbock", "TX", "79401", "123 Main St", "LUBBOCK", "TX", "79401")
    assert r.classification == SAME_ADDRESS


def test_state_change_is_highly_material():
    r = compare("123 Main St", "Lubbock", "TX", "79401", "123 Main St", "Lubbock", "OK", "79401")
    assert r.classification == LIKELY_CHANGE
    assert r.differences["state"] == "CHANGED"


def test_zip5_change_is_material():
    r = compare("123 Main St", "Lubbock", "TX", "79401", "123 Main St", "Lubbock", "TX", "79408")
    assert r.classification == LIKELY_CHANGE


def test_blank_new_address_is_insufficient_data_not_a_change():
    r = compare("123 Main St", "Lubbock", "TX", "79401", None, None, None, None)
    assert r.classification == INSUFFICIENT_DATA


def test_blank_current_address_is_insufficient_data():
    r = compare(None, None, None, None, "123 Main St", "Lubbock", "TX", "79401")
    assert r.classification == INSUFFICIENT_DATA


def test_both_blank_is_insufficient_data():
    r = compare(None, None, None, None, None, None, None, None)
    assert r.classification == INSUFFICIENT_DATA


def test_international_address_with_no_us_zip_does_not_crash():
    r = compare("123 Main St", "Toronto", "ON", None, "123 Main St", "Toronto", "ON", None)
    assert r.classification == SAME_ADDRESS


def test_international_address_change_still_detected():
    r = compare("123 Main St", "Toronto", "ON", None, "456 King St", "Toronto", "ON", None)
    assert r.classification == LIKELY_CHANGE


def test_differences_marks_zip_not_available_when_either_side_missing():
    r = compare("123 Main St", "Lubbock", "TX", None, "123 Main St", "Lubbock", "TX", "79401")
    assert r.differences["zip"] == "NOT AVAILABLE"
    # ZIP being unknown on one side alone must not, by itself, manufacture a
    # material change -- it's not confidently "the same" either (one side
    # genuinely lacks data), so it lands as non-material, not SAME_ADDRESS.
    assert r.classification in (SAME_ADDRESS, FORMAT_ONLY_DIFFERENCE)


def test_full_normalized_includes_unit_so_a_suite_addition_is_never_invisible():
    """Regression: full_normalized used to ignore `unit` entirely, so
    "123 Main St" and "123 Main St Ste 200" normalized to the identical
    string and the whole comparison short-circuited to SAME_ADDRESS before
    ever reaching the suite-handling branch."""

    from app.comptroller.address_normalizer import normalize_mailing_address

    a = normalize_mailing_address("123 Main St", city="Lubbock", state="TX", zip_code="79401")
    b = normalize_mailing_address("123 Main St Ste 200", city="Lubbock", state="TX", zip_code="79401")
    assert a.full_normalized != b.full_normalized
