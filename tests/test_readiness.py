from __future__ import annotations

import pytest

from app.comptroller import readiness
from app.comptroller.jurisdictions import Jurisdiction
from app.comptroller.readiness import BLOCKED, NOT_CONFIGURED, NOT_READY, OPTIONAL, READY, assess_production_readiness
from tests.comptroller_fakes import FakeSupabase


@pytest.fixture
def fake_supabase(monkeypatch):
    fake = FakeSupabase()
    monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "fake-service-role-key")
    monkeypatch.setattr(readiness, "_request_json", fake.request_json)
    return fake


def make_jurisdiction(**overrides) -> Jurisdiction:
    defaults = dict(
        id="jur-1", district_id="district-1", name="Test CAD", slug="test",
        county_name="Test", state="TX", timezone="America/Chicago", active=True,
        comptroller_county_code="999", comptroller_dataset_id="3kx8-uryv",
        capabilities={"new_business_detection": True}, cad_field_mapping={},
        property_field_mapping={}, appraiser_assignment_rules={},
    )
    defaults.update(overrides)
    return Jurisdiction(**defaults)


def check(result, name):
    return next(c for c in result.checks if c.name == name)


def test_all_blocked_with_nothing_configured(fake_supabase):
    jurisdiction = make_jurisdiction()
    result = assess_production_readiness(jurisdiction)
    assert check(result, "Comptroller data").status == NOT_READY
    assert check(result, "Persisted BPP accounts").status == NOT_READY
    assert check(result, "Property data").status == NOT_READY
    assert check(result, "Property field mapping").status == NOT_CONFIGURED
    assert check(result, "Current tax year").status == OPTIONAL
    assert check(result, "Appraiser rules").status == NOT_CONFIGURED
    assert check(result, "New Business Detection").status == READY  # name-only always works
    assert check(result, "High-confidence account corroboration").status == BLOCKED
    assert "no persisted BPP accounts" in check(result, "High-confidence account corroboration").detail
    assert check(result, "Property Enrichment").status == BLOCKED


def test_comptroller_data_ready_when_permits_exist(fake_supabase):
    jurisdiction = make_jurisdiction()
    fake_supabase.permit_locations["loc-1"] = {"id": "loc-1", "county": "Test", "taxpayer_id": "1", "location_number": "1"}
    result = assess_production_readiness(jurisdiction)
    assert check(result, "Comptroller data").status == READY


def test_persisted_accounts_ready_when_rows_exist(fake_supabase):
    jurisdiction = make_jurisdiction()
    fake_supabase.parsed_rendition_results["prr-1"] = {"id": "prr-1", "district_id": "district-1"}
    result = assess_production_readiness(jurisdiction)
    assert check(result, "Persisted BPP accounts").status == READY


def test_property_data_ready_when_rows_exist(fake_supabase):
    jurisdiction = make_jurisdiction()
    fake_supabase.real_property_records["r1"] = {"id": "r1", "jurisdiction_id": "jur-1"}
    result = assess_production_readiness(jurisdiction)
    assert check(result, "Property data").status == READY


def test_high_confidence_corroboration_ready_only_when_both_accounts_and_property_exist(fake_supabase):
    jurisdiction = make_jurisdiction()
    fake_supabase.parsed_rendition_results["prr-1"] = {"id": "prr-1", "district_id": "district-1"}
    result = assess_production_readiness(jurisdiction)
    assert check(result, "High-confidence account corroboration").status == BLOCKED
    assert "no property data" in check(result, "High-confidence account corroboration").detail

    fake_supabase.real_property_records["r1"] = {"id": "r1", "jurisdiction_id": "jur-1"}
    result2 = assess_production_readiness(jurisdiction)
    assert check(result2, "High-confidence account corroboration").status == READY


def test_property_field_mapping_degraded_when_only_optional_fields_missing(fake_supabase):
    jurisdiction = make_jurisdiction(
        capabilities={"new_business_detection": True, "real_property_linkage": True},
        property_field_mapping={"source_property_id": "PropertyID", "situs_address": "SitusAddress"},
    )
    result = assess_production_readiness(jurisdiction)
    assert check(result, "Property field mapping").status == "DEGRADED"


def test_property_field_mapping_not_ready_when_required_fields_missing(fake_supabase):
    jurisdiction = make_jurisdiction(
        capabilities={"new_business_detection": True, "real_property_linkage": True},
        property_field_mapping={"real_account_number": "QuickRefID"},
    )
    result = assess_production_readiness(jurisdiction)
    assert check(result, "Property field mapping").status == NOT_READY


def test_appraiser_rules_ready_when_configured(fake_supabase):
    jurisdiction = make_jurisdiction(appraiser_assignment_rules={"default": "queue@example.org"})
    result = assess_production_readiness(jurisdiction)
    assert check(result, "Appraiser rules").status == READY


def test_new_business_detection_blocked_without_capability_or_county_code(fake_supabase):
    jurisdiction = make_jurisdiction(capabilities={}, comptroller_county_code=None)
    result = assess_production_readiness(jurisdiction)
    assert check(result, "New Business Detection").status == BLOCKED


def test_jurisdiction_isolation_readiness_never_sees_another_jurisdictions_data(fake_supabase):
    jurisdiction_a = make_jurisdiction(id="jur-a", district_id="district-a")
    fake_supabase.parsed_rendition_results["prr-1"] = {"id": "prr-1", "district_id": "district-b"}
    fake_supabase.real_property_records["r1"] = {"id": "r1", "jurisdiction_id": "jur-b"}
    result = assess_production_readiness(jurisdiction_a)
    assert check(result, "Persisted BPP accounts").status == NOT_READY
    assert check(result, "Property data").status == NOT_READY


def test_mailing_address_monitoring_blocked_without_capability(fake_supabase):
    jurisdiction = make_jurisdiction(capabilities={})
    result = assess_production_readiness(jurisdiction)
    assert check(result, "Mailing Address Monitoring").status == BLOCKED


def test_mailing_address_monitoring_ready_with_capability_and_county_code(fake_supabase):
    jurisdiction = make_jurisdiction(capabilities={"mailing_address_monitoring": True})
    result = assess_production_readiness(jurisdiction)
    assert check(result, "Mailing Address Monitoring").status == READY


def test_current_cad_mailing_addresses_not_ready_with_no_observations(fake_supabase):
    jurisdiction = make_jurisdiction()
    result = assess_production_readiness(jurisdiction)
    assert check(result, "Current CAD mailing addresses").status == NOT_READY


def test_current_cad_mailing_addresses_ready_once_observed(fake_supabase):
    jurisdiction = make_jurisdiction()
    fake_supabase.mailing_address_observations["obs-1"] = {"id": "obs-1", "jurisdiction_id": "jur-1"}
    result = assess_production_readiness(jurisdiction)
    assert check(result, "Current CAD mailing addresses").status == READY


def test_rendition_mailing_addresses_always_not_ready_today(fake_supabase):
    """Honest limitation: OCR doesn't extract this field yet -- must never
    silently claim readiness it doesn't have."""

    jurisdiction = make_jurisdiction()
    result = assess_production_readiness(jurisdiction)
    assert check(result, "Rendition mailing addresses").status == NOT_READY


def test_mailing_address_jurisdiction_isolation(fake_supabase):
    jurisdiction_a = make_jurisdiction(id="jur-a")
    fake_supabase.mailing_address_observations["obs-1"] = {"id": "obs-1", "jurisdiction_id": "jur-b"}
    result = assess_production_readiness(jurisdiction_a)
    assert check(result, "Current CAD mailing addresses").status == NOT_READY
