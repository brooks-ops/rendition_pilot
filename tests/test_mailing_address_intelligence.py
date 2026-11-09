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


# Production scan regressions live in test_mailing_comparisons.py. These
# classification helpers remain supported for legacy evidence consumers.
