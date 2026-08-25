from __future__ import annotations

import pytest

from app.comptroller import jurisdictions, mailing_address_intelligence
from app.comptroller.jurisdictions import Jurisdiction
from app.comptroller.mailing_address_intelligence import (
    MailingAddressIntelligenceError,
    classify_mailing_address_change,
    run_mailing_address_intelligence,
)
from app.comptroller.mailing_address_matching import compare_mailing_addresses
from tests.comptroller_fakes import FakeSupabase


@pytest.fixture
def fake_supabase(monkeypatch):
    fake = FakeSupabase()
    monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "fake-service-role-key")
    monkeypatch.setattr(mailing_address_intelligence, "_request_json", fake.request_json)
    monkeypatch.setattr(jurisdictions, "_request_json", fake.request_json)
    return fake


def make_jurisdiction(**overrides) -> Jurisdiction:
    defaults = dict(
        id="jur-1", district_id="district-1", name="Test CAD", slug="test",
        county_name="Test", state="TX", timezone="America/Chicago", active=True,
        comptroller_county_code="999", comptroller_dataset_id="3kx8-uryv",
        capabilities={"mailing_address_monitoring": True},
        cad_field_mapping={}, property_field_mapping={}, appraiser_assignment_rules={},
    )
    defaults.update(overrides)
    return Jurisdiction(**defaults)


def seed_jurisdiction(fake_supabase, **overrides) -> Jurisdiction:
    jurisdiction = make_jurisdiction(**overrides)
    fake_supabase.jurisdictions[jurisdiction.id] = {
        "id": jurisdiction.id, "district_id": jurisdiction.district_id, "name": jurisdiction.name,
        "slug": jurisdiction.slug, "county_name": jurisdiction.county_name, "state": jurisdiction.state,
        "timezone": jurisdiction.timezone, "active": jurisdiction.active,
        "comptroller_county_code": jurisdiction.comptroller_county_code,
        "comptroller_dataset_id": jurisdiction.comptroller_dataset_id,
        "capabilities": jurisdiction.capabilities, "cad_field_mapping": jurisdiction.cad_field_mapping,
        "property_field_mapping": jurisdiction.property_field_mapping,
        "appraiser_assignment_rules": jurisdiction.appraiser_assignment_rules,
    }
    return jurisdiction


def add_permit(fake_supabase, taxpayer_id, loc, *, county="Test", legal_name="ACME LLC",
                mailing_address="PO Box 500", mailing_city="Lubbock", mailing_state="TX",
                mailing_zip="79401", current_status="ACTIVE"):
    key = f"{taxpayer_id}::{loc}"
    fake_supabase.permit_locations[key] = {
        "id": f"loc-{taxpayer_id}-{loc}", "taxpayer_id": taxpayer_id, "location_number": loc,
        "county": county, "legal_name": legal_name, "location_name": legal_name,
        "current_status": current_status,
        "mailing_address": mailing_address, "mailing_city": mailing_city,
        "mailing_state": mailing_state, "mailing_zip": mailing_zip, "mailing_zip4": None,
    }


# -- classify_mailing_address_change -----------------------------------------

def test_classify_no_alert_for_same_address():
    comparison = compare_mailing_addresses(
        current_raw="123 Main St", current_city="Lubbock", current_state="TX", current_zip="79401",
        observed_raw="123 Main Street", observed_city="Lubbock", observed_state="TX", observed_zip="79401",
    )
    classification, priority, confidence = classify_mailing_address_change(
        comparison, identity_confidence="HIGH", source="tx_comptroller_open_data",
    )
    assert classification is None


def test_classify_likely_change_high_identity_and_high_change_confidence():
    comparison = compare_mailing_addresses(
        current_raw="123 Main St", current_city="Lubbock", current_state="TX", current_zip="79401",
        observed_raw="PO Box 900", observed_city="Lubbock", observed_state="TX", observed_zip="79401",
    )
    classification, priority, confidence = classify_mailing_address_change(
        comparison, identity_confidence="HIGH", source="tx_comptroller_open_data",
    )
    assert classification == "LIKELY_MAILING_ADDRESS_CHANGE"
    assert priority == "HIGH"
    assert confidence == "HIGH"


def test_classify_confirmed_when_source_is_high_trust():
    comparison = compare_mailing_addresses(
        current_raw="123 Main St", current_city="Lubbock", current_state="TX", current_zip="79401",
        observed_raw="PO Box 900", observed_city="Lubbock", observed_state="TX", observed_zip="79401",
    )
    classification, priority, confidence = classify_mailing_address_change(
        comparison, identity_confidence="HIGH", source="rendition_submitted_by_taxpayer",
    )
    assert classification == "CONFIRMED_MAILING_ADDRESS_CHANGE"


def test_classify_weak_identity_caps_confidence_even_with_clear_change():
    """Spec item 29: a perfect address difference attached to a weak
    identity match must never present as HIGH confidence."""

    comparison = compare_mailing_addresses(
        current_raw="123 Main St", current_city="Lubbock", current_state="TX", current_zip="79401",
        observed_raw="PO Box 900", observed_city="Lubbock", observed_state="TX", observed_zip="79401",
    )
    classification, priority, confidence = classify_mailing_address_change(
        comparison, identity_confidence="LOW", source="tx_comptroller_open_data",
    )
    assert classification == "POSSIBLE_MAILING_ADDRESS_CHANGE"
    assert confidence == "LOW"


def test_classify_possible_change_stays_possible_even_with_high_identity():
    comparison = compare_mailing_addresses(
        current_raw="123 Main St", current_city="Lubbock", current_state="TX", current_zip="79401",
        observed_raw="123 Main St Ste 200", observed_city="Lubbock", observed_state="TX", observed_zip="79401",
    )
    classification, priority, confidence = classify_mailing_address_change(
        comparison, identity_confidence="HIGH", source="tx_comptroller_open_data",
    )
    assert classification == "POSSIBLE_MAILING_ADDRESS_CHANGE"
    assert priority == "MEDIUM"


# -- run_mailing_address_intelligence ----------------------------------------

def test_raises_when_capability_not_enabled(fake_supabase):
    jurisdiction = seed_jurisdiction(fake_supabase, capabilities={})
    with pytest.raises(MailingAddressIntelligenceError):
        run_mailing_address_intelligence(jurisdiction.id)


def test_first_observation_establishes_baseline_with_no_alert(fake_supabase):
    jurisdiction = seed_jurisdiction(fake_supabase)
    add_permit(fake_supabase, "TP1", "1")

    result = run_mailing_address_intelligence(jurisdiction.id)

    assert result.evaluated == 1
    assert result.baseline_established == 1
    assert result.items_created == 0
    assert len(fake_supabase.mailing_address_observations) == 1


def test_second_run_with_unchanged_address_creates_no_alert(fake_supabase):
    jurisdiction = seed_jurisdiction(fake_supabase)
    add_permit(fake_supabase, "TP1", "1")
    run_mailing_address_intelligence(jurisdiction.id)  # baseline
    result = run_mailing_address_intelligence(jurisdiction.id)  # re-run, same address

    assert result.same_address == 1
    assert result.items_created == 0
    assert len(fake_supabase.mailing_address_observations) == 1  # no duplicate history row


def test_material_change_creates_intelligence_item(fake_supabase):
    jurisdiction = seed_jurisdiction(fake_supabase)
    add_permit(fake_supabase, "TP1", "1", mailing_address="123 Main St")
    run_mailing_address_intelligence(jurisdiction.id)  # baseline: 123 Main St

    fake_supabase.permit_locations["TP1::1"]["mailing_address"] = "PO Box 900"
    result = run_mailing_address_intelligence(jurisdiction.id)

    assert result.likely_change == 1
    assert result.items_created == 1
    item = next(iter(fake_supabase.intelligence_items.values()))
    assert item["signal_type"] == "mailing_address_change"
    assert item["classification"] == "LIKELY_MAILING_ADDRESS_CHANGE"
    assert item["mailing_address_current"] is not None
    assert item["mailing_address_observed"] is not None
    assert len(fake_supabase.mailing_address_observations) == 2  # both addresses preserved as history


def test_formatting_only_difference_creates_no_alert(fake_supabase):
    jurisdiction = seed_jurisdiction(fake_supabase)
    add_permit(fake_supabase, "TP1", "1", mailing_address="123 Main St")
    run_mailing_address_intelligence(jurisdiction.id)

    fake_supabase.permit_locations["TP1::1"]["mailing_address"] = "123 Main Street"
    result = run_mailing_address_intelligence(jurisdiction.id)

    assert result.same_address == 1
    assert result.items_created == 0


def test_sequential_different_changes_are_not_suppressed(fake_supabase):
    """Spec item 20: a genuinely newer address must not be suppressed just
    because an earlier change already has an intelligence item on file."""

    jurisdiction = seed_jurisdiction(fake_supabase)
    add_permit(fake_supabase, "TP1", "1", mailing_address="Address A")
    run_mailing_address_intelligence(jurisdiction.id)  # baseline: Address A

    fake_supabase.permit_locations["TP1::1"]["mailing_address"] = "Address B"
    run_mailing_address_intelligence(jurisdiction.id)  # A -> B, alert #1

    fake_supabase.permit_locations["TP1::1"]["mailing_address"] = "Address C"
    run_mailing_address_intelligence(jurisdiction.id)  # B -> C, alert #2

    assert len(fake_supabase.intelligence_items) == 2
    assert len(fake_supabase.mailing_address_observations) == 3


def test_resolved_item_is_never_reopened_or_duplicated(fake_supabase):
    """Since "current known" always advances to the latest observation
    after each run, the same transition is never re-detected under normal
    operation -- this simulates the realistic case where it CAN recur: the
    specific observation that recorded the new address is later lost (a
    manual correction, a reimport), so the next run re-derives the exact
    same (taxpayer, observed-address) transition and must not reopen or
    duplicate the already-resolved decision."""

    jurisdiction = seed_jurisdiction(fake_supabase)
    add_permit(fake_supabase, "TP1", "1", mailing_address="123 Main St")
    run_mailing_address_intelligence(jurisdiction.id)  # baseline: 123 Main St

    fake_supabase.permit_locations["TP1::1"]["mailing_address"] = "PO Box 900"
    result = run_mailing_address_intelligence(jurisdiction.id)  # 123 Main St -> PO Box 900
    item_id = result.item_ids[0]
    fake_supabase.intelligence_items[item_id]["status"] = "RESOLVED"

    # Drop the "PO Box 900" observation so the latest remaining one is
    # "123 Main St" again -- the next run re-derives the identical transition.
    stale_key = next(
        k for k, row in fake_supabase.mailing_address_observations.items()
        if row["account_identifier"] == "TP1" and "PO BOX 900" in row["normalized_full_address"]
    )
    del fake_supabase.mailing_address_observations[stale_key]

    result2 = run_mailing_address_intelligence(jurisdiction.id)

    assert result2.duplicates_suppressed == 1
    assert fake_supabase.intelligence_items[item_id]["status"] == "RESOLVED"


def test_dry_run_writes_nothing(fake_supabase):
    jurisdiction = seed_jurisdiction(fake_supabase)
    add_permit(fake_supabase, "TP1", "1", mailing_address="123 Main St")
    run_mailing_address_intelligence(jurisdiction.id)

    fake_supabase.permit_locations["TP1::1"]["mailing_address"] = "PO Box 900"
    result = run_mailing_address_intelligence(jurisdiction.id, dry_run=True)

    assert result.likely_change == 1
    assert fake_supabase.intelligence_items == {}
    assert len(fake_supabase.mailing_address_observations) == 1  # only the baseline from the first (non-dry) run


def test_jurisdiction_isolation(fake_supabase):
    jurisdiction_a = seed_jurisdiction(fake_supabase, id="jur-a")
    jurisdiction_b = seed_jurisdiction(fake_supabase, id="jur-b", county_name="OtherCounty")
    add_permit(fake_supabase, "TP1", "1", county="Test", mailing_address="123 Main St")
    add_permit(fake_supabase, "TP2", "1", county="OtherCounty", mailing_address="456 Oak Ave")

    result_a = run_mailing_address_intelligence("jur-a")
    result_b = run_mailing_address_intelligence("jur-b")

    assert result_a.evaluated == 1
    assert result_b.evaluated == 1
    a_obs = [o for o in fake_supabase.mailing_address_observations.values() if o["jurisdiction_id"] == "jur-a"]
    b_obs = [o for o in fake_supabase.mailing_address_observations.values() if o["jurisdiction_id"] == "jur-b"]
    assert len(a_obs) == 1 and a_obs[0]["account_identifier"] == "TP1"
    assert len(b_obs) == 1 and b_obs[0]["account_identifier"] == "TP2"


def test_multiple_locations_same_taxpayer_deduped_to_one_candidate(fake_supabase):
    jurisdiction = seed_jurisdiction(fake_supabase)
    add_permit(fake_supabase, "TP1", "1", mailing_address="PO Box 500")
    add_permit(fake_supabase, "TP1", "2", mailing_address="PO Box 500")

    result = run_mailing_address_intelligence(jurisdiction.id)

    assert result.evaluated == 1  # one taxpayer, not two location rows


def test_inactive_permits_excluded(fake_supabase):
    jurisdiction = seed_jurisdiction(fake_supabase)
    add_permit(fake_supabase, "TP1", "1", current_status="INACTIVE")

    result = run_mailing_address_intelligence(jurisdiction.id)

    assert result.evaluated == 0


def test_blank_mailing_address_is_skipped_not_flagged(fake_supabase):
    jurisdiction = seed_jurisdiction(fake_supabase)
    add_permit(fake_supabase, "TP1", "1", mailing_address=None, mailing_city=None, mailing_state=None, mailing_zip=None)

    result = run_mailing_address_intelligence(jurisdiction.id)

    assert result.baseline_established == 0
    assert result.insufficient_data == 1
    assert len(fake_supabase.mailing_address_observations) == 0
