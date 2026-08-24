from __future__ import annotations

import pytest

from app.comptroller import matching


class FakeMatchResponse:
    def __init__(self, rows):
        self.status_code = 200
        self._rows = rows
        self.text = ""

    def json(self):
        return self._rows


@pytest.fixture(autouse=True)
def supabase_env(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "fake-service-role-key")


def set_records(monkeypatch, rows):
    monkeypatch.setattr(matching.requests, "get", lambda *args, **kwargs: FakeMatchResponse(rows))


def record_row(record_id="rec-1", account_number="R100001", owner_name="ACME HARDWARE LLC", tax_year=2026):
    return {
        "record_id": record_id,
        "account_number": account_number,
        "owner_name": owner_name,
        "tax_year": tax_year,
    }


# -- normalization -------------------------------------------------------


def test_normalize_name_strips_business_suffixes():
    assert matching.normalize_name("Acme Hardware, Inc.") == matching.normalize_name("ACME HARDWARE")


def test_normalize_name_strips_llc_and_punctuation():
    assert matching.normalize_name("Joe's Sports Bar, LLC") == matching.normalize_name("JOES SPORTS BAR")


# -- strong name match (MEDIUM: the only reachable non-zero confidence today) --


def test_strong_owner_name_match_is_medium_not_high(monkeypatch):
    """HIGH is intentionally unreachable: RenditionPilot has no address/ZIP/
    cross-referenced ID to corroborate a name match against (see
    matching.py's module docstring), so even a strong name match caps at
    MEDIUM."""

    set_records(monkeypatch, [record_row(owner_name="ACME HARDWARE")])

    result = matching.match_closure_to_account(
        district_id="district-1",
        permit_legal_name="ACME HARDWARE LLC",
        permit_location_name="ACME HARDWARE",
    )

    assert result.confidence == "MEDIUM"
    assert result.candidate is not None
    assert result.candidate.record_id == "rec-1"
    assert "strong owner-name match" in result.reason
    assert result.ambiguous is False


def test_partial_owner_name_match_is_low(monkeypatch):
    set_records(monkeypatch, [record_row(owner_name="ACME HARDWARE AND SUPPLY COMPANY")])

    result = matching.match_closure_to_account(
        district_id="district-1",
        permit_legal_name="ACME HARDWARE LLC",
        permit_location_name="ACME HARDWARE",
    )

    assert result.confidence == "LOW"
    assert result.candidate is not None


def test_dba_differing_from_legal_name_still_matches_via_legal_name(monkeypatch):
    """XYZ HOSPITALITY LLC operating as JOE'S SPORTS BAR: RenditionPilot's
    owner_name might reflect either the legal entity or the DBA depending on
    what was printed on the rendition form. The matcher checks both of the
    Comptroller closure's names against RenditionPilot's single owner_name
    field, so a match via the legal name should succeed even though the DBA
    looks nothing like it."""

    set_records(monkeypatch, [record_row(owner_name="XYZ HOSPITALITY LLC")])

    result = matching.match_closure_to_account(
        district_id="district-1",
        permit_legal_name="XYZ HOSPITALITY LLC",
        permit_location_name="JOE'S SPORTS BAR",
    )

    assert result.confidence == "MEDIUM"
    assert result.candidate.record_id == "rec-1"


# -- ambiguous: same/similar name appears on multiple RenditionPilot records --


def test_same_owner_name_on_multiple_records_is_flagged_ambiguous(monkeypatch):
    """Without address data, two rendition records for the same owner name
    (e.g. different tax years, or a chain operator) can't be told apart --
    the matcher must not silently pick one and call it confident."""

    set_records(
        monkeypatch,
        [
            record_row(record_id="rec-2025", owner_name="ACME HARDWARE", tax_year=2025),
            record_row(record_id="rec-2026", owner_name="ACME HARDWARE", tax_year=2026),
        ],
    )

    result = matching.match_closure_to_account(
        district_id="district-1",
        permit_legal_name="ACME HARDWARE LLC",
        permit_location_name="ACME HARDWARE",
    )

    assert result.confidence == "MEDIUM"
    assert result.ambiguous is True
    assert "ambiguous" in result.reason


def test_distinct_owner_names_are_not_flagged_ambiguous(monkeypatch):
    set_records(
        monkeypatch,
        [
            record_row(record_id="rec-1", owner_name="ACME HARDWARE"),
            record_row(record_id="rec-2", owner_name="TOTALLY UNRELATED BUSINESS"),
        ],
    )

    result = matching.match_closure_to_account(
        district_id="district-1",
        permit_legal_name="ACME HARDWARE LLC",
        permit_location_name="ACME HARDWARE",
    )

    assert result.ambiguous is False
    assert result.candidate.record_id == "rec-1"


# -- unmatched ----------------------------------------------------------------


def test_unmatched_when_no_candidate_records(monkeypatch):
    set_records(monkeypatch, [])

    result = matching.match_closure_to_account(
        district_id="district-1",
        permit_legal_name="ACME HARDWARE LLC",
        permit_location_name="ACME HARDWARE",
    )

    assert result.confidence == "UNMATCHED"
    assert result.candidate is None


def test_unmatched_when_no_district_mapping_available(monkeypatch):
    def fail_get(*args, **kwargs):
        raise AssertionError("should not query rendition records without a district_id")

    monkeypatch.setattr(matching.requests, "get", fail_get)

    result = matching.match_closure_to_account(
        district_id=None,
        permit_legal_name="ACME HARDWARE LLC",
        permit_location_name="ACME HARDWARE",
    )

    assert result.confidence == "UNMATCHED"
    assert result.candidate is None


def test_unmatched_when_name_similarity_below_threshold(monkeypatch):
    set_records(monkeypatch, [record_row(owner_name="TOTALLY UNRELATED BUSINESS")])

    result = matching.match_closure_to_account(
        district_id="district-1",
        permit_legal_name="ACME HARDWARE LLC",
        permit_location_name="ACME HARDWARE",
    )

    assert result.confidence == "UNMATCHED"
    assert result.candidate is None


def test_address_signature_args_are_accepted_and_ignored(monkeypatch):
    """RenditionPilot has no address data; callers built against the earlier
    address-aware signature should still work without raising."""

    set_records(monkeypatch, [record_row(owner_name="ACME HARDWARE")])

    result = matching.match_closure_to_account(
        district_id="district-1",
        permit_legal_name="ACME HARDWARE LLC",
        permit_location_name="ACME HARDWARE",
        permit_address="100 MAIN ST",
        permit_city="LUBBOCK",
        permit_zip="79401",
    )

    assert result.confidence == "MEDIUM"


# -- transparent signal breakdown (spec: avoid unexplained black-box scores) --


def test_signals_mark_unavailable_fields_explicitly_not_as_no_match(monkeypatch):
    set_records(monkeypatch, [record_row(owner_name="ACME HARDWARE")])

    result = matching.match_closure_to_account(
        district_id="district-1", permit_legal_name="ACME HARDWARE LLC", permit_location_name="ACME HARDWARE",
    )

    assert "NOT AVAILABLE" in result.signals["address"]
    assert "NOT AVAILABLE" in result.signals["zip"]
    assert "NOT AVAILABLE" in result.signals["suite_unit"]
    assert "NOT AVAILABLE" in result.signals["property_account"]


def test_signals_report_match_for_strong_name(monkeypatch):
    set_records(monkeypatch, [record_row(owner_name="ACME HARDWARE")])

    result = matching.match_closure_to_account(
        district_id="district-1", permit_legal_name="ACME HARDWARE LLC", permit_location_name="ACME HARDWARE",
    )

    assert result.signals["business_dba_name"] == "MATCH"
    assert "FOUND" in result.signals["existing_rendition_record"]


def test_signals_are_none_when_unmatched(monkeypatch):
    set_records(monkeypatch, [])

    result = matching.match_closure_to_account(
        district_id="district-1", permit_legal_name="ACME HARDWARE LLC", permit_location_name="ACME HARDWARE",
    )

    assert result.signals["business_dba_name"] == "NO MATCH"
    assert result.signals["existing_rendition_record"] == "NONE"


# -- ownership-change hint: DBA/legal name divergence ------------------------


def test_name_divergence_flagged_when_dba_matches_but_legal_name_does_not(monkeypatch):
    set_records(monkeypatch, [record_row(owner_name="JOES SPORTS BAR")])

    result = matching.match_closure_to_account(
        district_id="district-1",
        permit_legal_name="XYZ HOSPITALITY LLC",
        permit_location_name="JOE'S SPORTS BAR",
    )

    assert result.name_signals_diverge is True
    assert "ownership change" in result.reason


def test_name_divergence_not_flagged_when_both_names_agree(monkeypatch):
    set_records(monkeypatch, [record_row(owner_name="ACME HARDWARE")])

    result = matching.match_closure_to_account(
        district_id="district-1",
        permit_legal_name="ACME HARDWARE LLC",
        permit_location_name="ACME HARDWARE",
    )

    assert result.name_signals_diverge is False


def test_name_divergence_not_flagged_when_neither_name_matches(monkeypatch):
    set_records(monkeypatch, [record_row(owner_name="TOTALLY UNRELATED CO")])

    result = matching.match_closure_to_account(
        district_id="district-1",
        permit_legal_name="ACME HARDWARE LLC",
        permit_location_name="ACME HARDWARE",
    )

    assert result.name_signals_diverge is False


# -- property corroboration (HIGH confidence) --------------------------------
#
# Regression coverage for a real bug: a BPP rendition's account_number is
# always P-style (the number printed on the rendition), while a matched
# property's own real_account_number is always R-style (the land record) --
# a real Texas CAD export mixes both under one column. Comparing a
# candidate's account_number against the R-account would never match, by
# definition, making HIGH permanently unreachable even with perfect data.
# Corroboration must compare against property_match.personal_property_accounts
# instead. See app/comptroller/property_matching.classify_account_type.

def make_property_match(*, classification="EXACT_PROPERTY_MATCH", personal_property_accounts=None):
    from app.comptroller.property_matching import PropertyMatchResult

    return PropertyMatchResult(
        classification=classification, confidence="HIGH", score=1.0,
        matched_property=None, candidate_count=1,
        personal_property_accounts=personal_property_accounts or [],
    )


def test_high_confidence_reachable_when_account_matches_personal_property_account(monkeypatch):
    set_records(monkeypatch, [record_row(account_number="P700000", owner_name="ACME HARDWARE LLC")])

    result = matching.match_closure_to_account(
        district_id="district-1", permit_legal_name="ACME HARDWARE LLC", permit_location_name="ACME HARDWARE",
        property_match=make_property_match(personal_property_accounts=["P700000"]),
    )

    assert result.confidence == "HIGH"
    assert "corroborated" in result.reason


def test_high_confidence_not_reached_when_compared_against_real_account_number(monkeypatch):
    """Pins the actual bug: the candidate's P-account must never be checked
    against a property's R-account, even when they happen to look similar,
    and even with an otherwise-perfect address+name match."""

    set_records(monkeypatch, [record_row(account_number="R500000", owner_name="ACME HARDWARE LLC")])

    result = matching.match_closure_to_account(
        district_id="district-1", permit_legal_name="ACME HARDWARE LLC", permit_location_name="ACME HARDWARE",
        # No personal_property_accounts at all -- only a real-property match.
        property_match=make_property_match(personal_property_accounts=[]),
    )

    assert result.confidence != "HIGH"


def test_high_confidence_not_reached_when_personal_account_does_not_match(monkeypatch):
    set_records(monkeypatch, [record_row(account_number="P700000", owner_name="ACME HARDWARE LLC")])

    result = matching.match_closure_to_account(
        district_id="district-1", permit_legal_name="ACME HARDWARE LLC", permit_location_name="ACME HARDWARE",
        property_match=make_property_match(personal_property_accounts=["P999999"]),
    )

    assert result.confidence != "HIGH"


def test_high_confidence_not_reached_from_possible_property_match(monkeypatch):
    set_records(monkeypatch, [record_row(account_number="P700000", owner_name="ACME HARDWARE LLC")])

    result = matching.match_closure_to_account(
        district_id="district-1", permit_legal_name="ACME HARDWARE LLC", permit_location_name="ACME HARDWARE",
        property_match=make_property_match(classification="POSSIBLE_PROPERTY_MATCH", personal_property_accounts=["P700000"]),
    )

    assert result.confidence != "HIGH"
