from __future__ import annotations

import pytest

from app.comptroller import new_account_enrichment, property_adapter
from app.comptroller.intelligence import SOURCE_TABLE_CLOSURE_REVIEW, SOURCE_TABLE_INTELLIGENCE, UnifiedIntelligenceItem
from app.comptroller.jurisdictions import Jurisdiction
from app.comptroller.new_account_enrichment import (
    NewAccountEnrichmentError,
    assign_appraiser,
    build_account_card,
    generate_account_card,
)
from tests.comptroller_fakes import FakeSupabase


@pytest.fixture
def fake_supabase(monkeypatch):
    fake = FakeSupabase()
    monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "fake-service-role-key")
    monkeypatch.setattr(new_account_enrichment, "_request_json", fake.request_json)
    monkeypatch.setattr(property_adapter, "_request_json", fake.request_json)
    return fake


def make_jurisdiction(**overrides) -> Jurisdiction:
    defaults = dict(
        id="jur-1", district_id="district-1", name="Test CAD", slug="test",
        county_name="Test", state="TX", timezone="America/Chicago", active=True,
        comptroller_county_code="999", comptroller_dataset_id="3kx8-uryv",
        capabilities={}, cad_field_mapping={}, property_field_mapping={},
        appraiser_assignment_rules={},
    )
    defaults.update(overrides)
    return Jurisdiction(**defaults)


def make_item(**overrides) -> UnifiedIntelligenceItem:
    defaults = dict(
        id="intel-1", source_table=SOURCE_TABLE_INTELLIGENCE, signal_type="new_business",
        status="NEW", classification="NO_ACCOUNT_FOUND", priority="HIGH", confidence="UNMATCHED",
        confidence_score=0.0, is_ambiguous=False, business_name="JOE'S SPORTS BAR", legal_name="JOE'S SPORTS BAR LLC",
        source_address="1234 MAIN ST", source_city="LUBBOCK", source_state="TX", source_zip="79401",
        permit_start_date="2026-08-01", permit_end_date=None, first_detected_at="2026-08-15T00:00:00Z",
        matched_account_number=None, matched_owner_name=None, match_reason=None, match_signals=None,
        recommended_action=None, resolution=None, resolution_notes=None, reviewed_by=None, reviewed_at=None,
        district_id="district-1", jurisdiction_id="jur-1", created_at="2026-08-15T00:00:00Z", raw={},
        property_match_status=None, matched_address=None, property_account_number=None,
        tug=None, neighborhood=None, map_id=None,
    )
    defaults.update(overrides)
    return UnifiedIntelligenceItem(**defaults)


# -- assign_appraiser ---------------------------------------------------------

def test_assign_appraiser_by_tug_takes_precedence():
    jurisdiction = make_jurisdiction(appraiser_assignment_rules={
        "by_tug": {"4": "tug-appraiser@example.org"},
        "by_neighborhood": {"2200": "nbhd-appraiser@example.org"},
    })
    result = assign_appraiser(jurisdiction, tug="4", neighborhood="2200")
    assert result.appraiser == "tug-appraiser@example.org"
    assert result.basis == "tug"


def test_assign_appraiser_falls_back_to_neighborhood():
    jurisdiction = make_jurisdiction(appraiser_assignment_rules={
        "by_neighborhood": {"2200": "nbhd-appraiser@example.org"},
    })
    result = assign_appraiser(jurisdiction, tug=None, neighborhood="2200")
    assert result.appraiser == "nbhd-appraiser@example.org"
    assert result.basis == "neighborhood"


def test_assign_appraiser_falls_back_to_default():
    jurisdiction = make_jurisdiction(appraiser_assignment_rules={"default": "queue@example.org"})
    result = assign_appraiser(jurisdiction, tug="99", neighborhood="9999")
    assert result.appraiser == "queue@example.org"
    assert result.basis == "default"


def test_assign_appraiser_unassigned_with_no_rules():
    jurisdiction = make_jurisdiction(appraiser_assignment_rules={})
    result = assign_appraiser(jurisdiction, tug="4", neighborhood="2200")
    assert result.appraiser is None
    assert result.basis == "unassigned"


def test_assign_appraiser_unassigned_when_tug_and_neighborhood_missing():
    jurisdiction = make_jurisdiction(appraiser_assignment_rules={"by_tug": {"4": "x@example.org"}})
    result = assign_appraiser(jurisdiction, tug=None, neighborhood=None)
    assert result.basis == "unassigned"


# -- build_account_card -------------------------------------------------------

def test_build_account_card_rejects_non_new_business_items(fake_supabase):
    jurisdiction = make_jurisdiction()
    item = make_item(source_table=SOURCE_TABLE_CLOSURE_REVIEW, signal_type="sales_tax_inactive")
    with pytest.raises(NewAccountEnrichmentError):
        build_account_card(item, jurisdiction)


def test_build_account_card_degrades_gracefully_with_no_property_match(fake_supabase):
    jurisdiction = make_jurisdiction()
    item = make_item()  # no property fields set
    card = build_account_card(item, jurisdiction)
    assert card.situs_address is None
    assert card.real_account_number is None
    assert card.tug is None
    assert card.appraiser_assignment.basis == "unassigned"
    assert card.business_name == "JOE'S SPORTS BAR"
    assert any("NO PROPERTY MATCH" in e for e in card.exceptions)
    assert any("APPRAISER UNASSIGNED" in e for e in card.exceptions)


def test_build_account_card_surfaces_ambiguous_property_match(fake_supabase):
    jurisdiction = make_jurisdiction()
    item = make_item(property_match_status="AMBIGUOUS_PROPERTY_MATCH")
    card = build_account_card(item, jurisdiction)
    assert any("PROPERTY MATCH AMBIGUOUS" in e for e in card.exceptions)
    assert not any("NO PROPERTY MATCH" in e for e in card.exceptions)


def test_build_account_card_surfaces_blank_real_account_number(fake_supabase):
    jurisdiction = make_jurisdiction()
    item = make_item(
        property_match_status="EXACT_PROPERTY_MATCH", matched_address="1234 MAIN ST",
        property_account_number=None,  # property matched, but QuickRefID was blank
    )
    card = build_account_card(item, jurisdiction)
    assert any("NO REAL ACCOUNT NUMBER" in e for e in card.exceptions)


def test_build_account_card_no_exceptions_when_everything_resolved(fake_supabase):
    jurisdiction = make_jurisdiction(appraiser_assignment_rules={"by_tug": {"4": "appraiser@example.org"}})
    item = make_item(
        property_match_status="EXACT_PROPERTY_MATCH", matched_address="1234 MAIN ST",
        property_account_number="R500000", tug="4",
    )
    card = build_account_card(item, jurisdiction)
    assert card.exceptions == []


def test_build_account_card_flags_existing_personal_property_account(fake_supabase):
    """Spec: 'if there is a P account (personal property) it needs to flag
    that.' A P-account already on file at this address is a loud, explicit
    exception -- the strongest available 'this might not be new' signal
    before real rendition data exists to name-match against."""

    jurisdiction = make_jurisdiction(appraiser_assignment_rules={"by_tug": {"4": "appraiser@example.org"}})
    item = make_item(
        property_match_status="EXACT_PROPERTY_MATCH", matched_address="1234 MAIN ST",
        property_account_number="R500000", tug="4",
        match_signals={"personal_property_accounts": "P302866"},
    )
    card = build_account_card(item, jurisdiction)
    assert card.personal_property_accounts == ["P302866"]
    assert any("EXISTING P-ACCOUNT FOUND" in e and "P302866" in e for e in card.exceptions)


def test_build_account_card_flags_multiple_personal_property_accounts(fake_supabase):
    jurisdiction = make_jurisdiction()
    item = make_item(match_signals={"personal_property_accounts": "P100001, P100002"})
    card = build_account_card(item, jurisdiction)
    assert card.personal_property_accounts == ["P100001", "P100002"]


def test_build_account_card_no_personal_account_exception_when_none_found(fake_supabase):
    jurisdiction = make_jurisdiction(appraiser_assignment_rules={"by_tug": {"4": "appraiser@example.org"}})
    item = make_item(
        property_match_status="EXACT_PROPERTY_MATCH", matched_address="1234 MAIN ST",
        property_account_number="R500000", tug="4",
        match_signals={"personal_property_accounts": "NONE FOUND"},
    )
    card = build_account_card(item, jurisdiction)
    assert card.personal_property_accounts == []
    assert not any("P-ACCOUNT" in e for e in card.exceptions)


def test_build_account_card_no_personal_account_exception_when_signals_missing(fake_supabase):
    """match_signals is None for items created before this feature existed --
    must degrade gracefully, not crash."""

    jurisdiction = make_jurisdiction()
    item = make_item(match_signals=None)
    card = build_account_card(item, jurisdiction)
    assert card.personal_property_accounts == []


def test_build_account_card_uses_cached_property_fields_when_no_record_id(fake_supabase):
    jurisdiction = make_jurisdiction(appraiser_assignment_rules={"by_tug": {"4": "appraiser@example.org"}})
    item = make_item(
        property_match_status="EXACT_PROPERTY_MATCH", matched_address="1234 MAIN ST",
        property_account_number="R500000", tug="4", neighborhood="2200", map_id="M-1",
    )
    card = build_account_card(item, jurisdiction)
    assert card.real_account_number == "R500000"
    assert card.tug == "4"
    assert card.appraiser_assignment.appraiser == "appraiser@example.org"
    assert card.appraiser_assignment.basis == "tug"
    assert card.suggested_property_link == "R500000"
    assert "1234 MAIN ST" in card.suggested_property_link_reason


def test_build_account_card_refetches_fresh_property_record_when_available(fake_supabase):
    jurisdiction = make_jurisdiction(appraiser_assignment_rules={"by_neighborhood": {"9999": "fresh@example.org"}})
    fake_supabase.real_property_records["jur-1::P1"] = {
        "id": "row-1", "jurisdiction_id": "jur-1", "source_property_id": "P1",
        "real_account_number": "R500000", "situs_address_raw": "1234 MAIN ST",
        "situs_address_normalized": "1234 MAIN STREET", "situs_city": None, "situs_state": None,
        "situs_zip": None, "owner_name": None, "tug": "STALE", "neighborhood": "9999", "map_id": "M-1",
        "latitude": None, "longitude": None, "source_system": None, "source_import_id": None, "source_updated_at": None,
    }
    # Item's cached copy is stale (tug="4"); the live property record now says neighborhood 9999.
    item = make_item(
        property_match_status="EXACT_PROPERTY_MATCH", matched_address="1234 MAIN ST",
        property_account_number="R500000", tug="4", neighborhood="OLD", map_id="OLD",
        raw={"property_record_id": "row-1"},
    )
    card = build_account_card(item, jurisdiction)
    assert card.tug == "STALE"  # re-fetched from real_property_records, not the cached "4"
    assert card.neighborhood == "9999"
    assert card.appraiser_assignment.appraiser == "fresh@example.org"


def test_generate_account_card_marks_item_generated(fake_supabase):
    jurisdiction = make_jurisdiction()
    fake_supabase.intelligence_items["intel-1"] = {"id": "intel-1", "account_card_generated_at": None}
    item = make_item()
    generate_account_card(item, jurisdiction)
    assert fake_supabase.intelligence_items["intel-1"]["account_card_generated_at"] is not None


def test_generate_account_card_dry_run_does_not_mark_generated(fake_supabase):
    jurisdiction = make_jurisdiction()
    fake_supabase.intelligence_items["intel-1"] = {"id": "intel-1", "account_card_generated_at": None}
    item = make_item()
    generate_account_card(item, jurisdiction, dry_run=True)
    assert fake_supabase.intelligence_items["intel-1"]["account_card_generated_at"] is None
