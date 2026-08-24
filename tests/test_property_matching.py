from __future__ import annotations

from app.comptroller.property_adapter import NormalizedRealProperty
from app.comptroller.property_matching import match_property, normalize_account_number, score_property_candidate
from app.comptroller.address_normalizer import normalize_address


def make_property(pid, addr, zip_=None, acct=None, **overrides):
    defaults = dict(
        property_id=pid, jurisdiction_id="jur-1", source_property_id=pid, tax_year=None,
        real_account_number=acct, situs_address_raw=addr, situs_address_normalized=None,
        situs_city="LUBBOCK", situs_state="TX", situs_zip=zip_, owner_name=None,
        tug=None, neighborhood=None, map_id=None, latitude=None, longitude=None,
        source_system="test", source_import_id=None, source_updated_at=None,
    )
    defaults.update(overrides)
    return NormalizedRealProperty(**defaults)


def test_exact_address_match():
    candidates = [make_property("P1", "5807 88TH PL", "79424", "R163313")]
    result = match_property("5807 88th Pl", input_zip="79424", candidates=candidates)
    assert result.classification == "EXACT_PROPERTY_MATCH"
    assert result.confidence == "HIGH"
    assert result.matched_property.property_id == "P1"


def test_abbreviation_normalization_still_matches_exact():
    candidates = [make_property("P1", "100 W University Avenue", "79409")]
    result = match_property("100 W University Ave", input_zip="79409", candidates=candidates)
    assert result.classification == "EXACT_PROPERTY_MATCH"


def test_zip_mismatch_prevents_exact_match():
    candidates = [make_property("P1", "100 Main St", "79401")]
    result = match_property("100 Main St", input_zip="79999", candidates=candidates)
    assert result.classification != "EXACT_PROPERTY_MATCH"


def test_suite_exact_match_is_exact():
    candidates = [make_property("P1", "123 Main St Ste 200", "79401")]
    result = match_property("123 Main Street Suite 200", input_zip="79401", candidates=candidates)
    assert result.classification == "EXACT_PROPERTY_MATCH"


def test_suite_missing_on_input_is_strong_not_exact():
    candidates = [make_property("P1", "500 Broadway St Ste 400", "79401")]
    result = match_property("500 Broadway St", input_zip="79401", candidates=candidates)
    assert result.classification == "STRONG_PROPERTY_MATCH"
    assert result.matched_property.property_id == "P1"


def test_suite_conflict_downgrades_confidence():
    candidates = [make_property("P1", "500 Broadway St Ste 400", "79401")]
    result = match_property("500 Broadway St Ste 900", input_zip="79401", candidates=candidates)
    assert result.classification == "POSSIBLE_PROPERTY_MATCH"
    assert result.confidence == "LOW"
    cs = score_property_candidate(normalize_address("500 Broadway St Ste 900", zip_code="79401"), candidates[0])
    assert cs.suite_conflict is True


def test_multiple_properties_at_one_base_address_conflicting_suites_is_ambiguous_when_tied():
    candidates = [
        make_property("P1", "123 Main St Ste 100", "79401"),
        make_property("P2", "123 Main St Ste 200", "79401"),
    ]
    result = match_property("123 Main St Ste 999", input_zip="79401", candidates=candidates)
    assert result.classification == "AMBIGUOUS_PROPERTY_MATCH"
    assert {a.property_id for a in result.alternatives} == {"P1", "P2"}


def test_ambiguous_when_two_properties_share_a_name_no_tiebreaker():
    candidates = [
        make_property("P1", "200 University Ave", "79409"),
        make_property("P2", "200 University Av", "79409"),
    ]
    result = match_property("200 University Avenue", input_zip="79409", candidates=candidates)
    assert result.classification == "AMBIGUOUS_PROPERTY_MATCH"
    assert result.matched_property is None


def test_no_match_when_nothing_resembles_the_input():
    candidates = [make_property("P1", "100 Main St", "79401")]
    result = match_property("999 Nowhere Rd", input_zip="55555", candidates=candidates)
    assert result.classification == "NO_PROPERTY_MATCH"
    assert result.confidence == "NONE"


def test_no_match_when_no_candidates_available():
    result = match_property("100 Main St", candidates=[])
    assert result.classification == "NO_PROPERTY_MATCH"
    assert "No real-property records" in result.reasons[0]


def test_no_match_when_no_input_address():
    result = match_property(None, candidates=[make_property("P1", "100 Main St")])
    assert result.classification == "NO_PROPERTY_MATCH"
    assert result.confidence == "NONE"


def test_signals_breakdown_reports_every_field():
    candidates = [make_property("P1", "5807 88TH PL", "79424")]
    result = match_property("5807 88th Pl", input_zip="79424", candidates=candidates)
    assert result.signals["street_number"] == "MATCH"
    assert result.signals["street_name"] == "MATCH"
    assert result.signals["zip"] == "MATCH"
    assert result.signals["suite"] == "NOT APPLICABLE"


def test_zip_not_available_does_not_block_exact_match():
    candidates = [make_property("P1", "5807 88TH PL", None)]
    result = match_property("5807 88th Pl", candidates=candidates)
    assert result.classification == "EXACT_PROPERTY_MATCH"
    assert result.signals["zip"] == "NOT AVAILABLE"


def test_normalize_account_number_loose_equality():
    assert normalize_account_number("r-163313") == normalize_account_number("R163313")
    assert normalize_account_number(None) is None
    assert normalize_account_number("") is None
