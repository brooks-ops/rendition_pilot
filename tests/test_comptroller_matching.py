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
