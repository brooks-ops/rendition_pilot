"""New Business Detection + Property Enrichment integration: proves HIGH
confidence is reachable ONLY through genuine corroboration (strong name +
exact/strong property match + matching account number), never from address
alone and never from name alone -- spec items 13/14.
"""

from __future__ import annotations

import pytest

from app.comptroller import jurisdictions, matching, new_business, property_adapter, property_enrichment
from app.comptroller.jurisdictions import Jurisdiction
from tests.comptroller_fakes import FakeSupabase
from tests.test_comptroller_new_business import FakeAccountsResponse, add_permit, seed_jurisdiction


@pytest.fixture
def fake_supabase(monkeypatch):
    fake = FakeSupabase()
    monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "fake-service-role-key")
    monkeypatch.setattr(service_module(), "_request_json", fake.request_json)
    monkeypatch.setattr(new_business, "_request_json", fake.request_json)
    monkeypatch.setattr(jurisdictions, "_request_json", fake.request_json)
    monkeypatch.setattr(property_adapter, "_request_json", fake.request_json)
    monkeypatch.setattr(property_enrichment, "_request_json", fake.request_json)
    return fake


def service_module():
    from app.comptroller import service
    return service


def seed_property(fake_supabase, jurisdiction_id, pid, addr, zip_=None, acct=None):
    from app.comptroller.address_normalizer import normalize_address

    fake_supabase.real_property_records[f"{jurisdiction_id}::{pid}"] = {
        "id": f"row-{pid}", "jurisdiction_id": jurisdiction_id, "source_property_id": pid,
        "real_account_number": acct, "situs_address_raw": addr,
        "situs_address_normalized": normalize_address(addr, zip_code=zip_).normalized,
        "situs_city": None, "situs_state": None, "situs_zip": zip_, "owner_name": None,
        "tug": None, "neighborhood": None, "map_id": None,
        "latitude": None, "longitude": None, "source_system": "imported_file",
        "source_import_id": None, "source_updated_at": None,
    }


def seed_jurisdiction_with_property(fake_supabase, **overrides) -> Jurisdiction:
    overrides.setdefault("capabilities", {
        "sales_tax_monitoring": True, "new_business_detection": True, "real_property_linkage": True,
    })
    overrides.setdefault("property_field_mapping", {
        "source_property_id": "PropertyID", "real_account_number": "QuickRefID", "situs_address": "SitusAddress",
    })
    return seed_jurisdiction(fake_supabase, **overrides)


def test_high_confidence_reachable_with_strong_name_and_exact_property_and_matching_account(fake_supabase, monkeypatch):
    jurisdiction = seed_jurisdiction_with_property(fake_supabase)
    add_permit(
        fake_supabase, "TP1", "LOC1", legal_name="ACME HARDWARE LLC", location_name="ACME HARDWARE",
    )
    fake_supabase.permit_locations["TP1::LOC1"]["address"] = "100 MAIN ST"
    fake_supabase.permit_locations["TP1::LOC1"]["zip"] = "79401"
    seed_property(fake_supabase, jurisdiction.id, "P1", "100 MAIN ST", "79401", acct="R500000")
    monkeypatch.setattr(
        matching.requests, "get",
        lambda *a, **kw: FakeAccountsResponse([
            {"record_id": "acc-1", "account_number": "R500000", "owner_name": "ACME HARDWARE LLC", "tax_year": 2026},
        ]),
    )

    result = new_business.run_new_business_detection(jurisdiction.id)

    # HIGH confidence is reachable (the count landed in the right bucket);
    # a high-confidence existing-account match still creates no alert by
    # design (existing, unchanged behavior -- see run_new_business_detection).
    assert result.existing_high_confidence == 1
    assert result.items_created == 0
    assert fake_supabase.intelligence_items == {}


def test_exact_property_match_alone_does_not_produce_high_without_strong_name(fake_supabase, monkeypatch):
    jurisdiction = seed_jurisdiction_with_property(fake_supabase)
    add_permit(fake_supabase, "TP2", "LOC2", legal_name="COMPLETELY DIFFERENT NAME LLC", location_name="COMPLETELY DIFFERENT NAME")
    fake_supabase.permit_locations["TP2::LOC2"]["address"] = "200 MAIN ST"
    fake_supabase.permit_locations["TP2::LOC2"]["zip"] = "79401"
    seed_property(fake_supabase, jurisdiction.id, "P2", "200 MAIN ST", "79401", acct="R500001")
    monkeypatch.setattr(
        matching.requests, "get",
        lambda *a, **kw: FakeAccountsResponse([
            {"record_id": "acc-2", "account_number": "R500001", "owner_name": "UNRELATED BUSINESS NAME", "tax_year": 2026},
        ]),
    )

    result = new_business.run_new_business_detection(jurisdiction.id)

    assert result.existing_high_confidence == 0
    item = next(iter(fake_supabase.intelligence_items.values()))
    assert item["confidence"] != "HIGH"


def test_strong_name_alone_without_property_data_does_not_produce_high(fake_supabase, monkeypatch):
    # No property capability enabled at all -- exact reproduction of
    # pre-Property-Enrichment behavior for jurisdictions with no property data.
    jurisdiction = seed_jurisdiction(fake_supabase, capabilities={"new_business_detection": True})
    add_permit(fake_supabase, "TP3", "LOC3", legal_name="ACME HARDWARE LLC", location_name="ACME HARDWARE")
    monkeypatch.setattr(
        matching.requests, "get",
        lambda *a, **kw: FakeAccountsResponse([
            {"record_id": "acc-3", "account_number": "R999999", "owner_name": "ACME HARDWARE LLC", "tax_year": 2026},
        ]),
    )

    result = new_business.run_new_business_detection(jurisdiction.id)

    assert result.existing_high_confidence == 0
    item = next(iter(fake_supabase.intelligence_items.values()))
    assert item["confidence"] == "MEDIUM"
    assert item.get("property_match_status") is None


def test_strong_name_and_exact_address_but_mismatched_account_number_does_not_produce_high(fake_supabase, monkeypatch):
    # Address matches exactly and the property record exists, but its real
    # account number does NOT match the RenditionPilot candidate's account
    # number -- e.g. two different tenants' accounts at a shared address.
    # Address+name agreement alone must never be enough (spec item 14).
    jurisdiction = seed_jurisdiction_with_property(fake_supabase)
    add_permit(fake_supabase, "TP4", "LOC4", legal_name="ACME HARDWARE LLC", location_name="ACME HARDWARE")
    fake_supabase.permit_locations["TP4::LOC4"]["address"] = "300 MAIN ST"
    fake_supabase.permit_locations["TP4::LOC4"]["zip"] = "79401"
    seed_property(fake_supabase, jurisdiction.id, "P4", "300 MAIN ST", "79401", acct="R700000")
    monkeypatch.setattr(
        matching.requests, "get",
        lambda *a, **kw: FakeAccountsResponse([
            {"record_id": "acc-4", "account_number": "R999999", "owner_name": "ACME HARDWARE LLC", "tax_year": 2026},
        ]),
    )

    result = new_business.run_new_business_detection(jurisdiction.id)

    assert result.existing_high_confidence == 0
    item = next(iter(fake_supabase.intelligence_items.values()))
    assert item["confidence"] != "HIGH"
    assert item["property_match_status"] == "EXACT_PROPERTY_MATCH"


def test_property_enrichment_disabled_gracefully_when_mapping_incomplete(fake_supabase, monkeypatch):
    jurisdiction = seed_jurisdiction(
        fake_supabase,
        capabilities={"new_business_detection": True, "real_property_linkage": True},
        property_field_mapping={"real_account_number": "QuickRefID"},  # missing required fields
    )
    add_permit(fake_supabase, "TP5", "LOC5", legal_name="ACME HARDWARE LLC", location_name="ACME HARDWARE")
    fake_supabase.permit_locations["TP5::LOC5"]["address"] = "400 MAIN ST"
    monkeypatch.setattr(
        matching.requests, "get",
        lambda *a, **kw: FakeAccountsResponse([
            {"record_id": "acc-5", "account_number": "R1", "owner_name": "ACME HARDWARE LLC", "tax_year": 2026},
        ]),
    )

    # Must not raise -- an incomplete property mapping degrades to
    # name-only behavior rather than blocking New Business Detection.
    result = new_business.run_new_business_detection(jurisdiction.id)
    assert result.evaluated == 1


def test_property_enrichment_results_are_jurisdiction_scoped(fake_supabase, monkeypatch):
    seed_jurisdiction_with_property(fake_supabase, id="jur-a", district_id="district-a")
    jurisdiction_b_row = dict(fake_supabase.jurisdictions["jur-a"])
    jurisdiction_b_row.update({"id": "jur-b", "district_id": "district-b", "slug": "jur-b-slug", "county_name": "OtherCounty"})
    fake_supabase.jurisdictions["jur-b"] = jurisdiction_b_row

    add_permit(fake_supabase, "TP6", "LOC6", county="OtherCounty", legal_name="ACME HARDWARE LLC", location_name="ACME HARDWARE")
    fake_supabase.permit_locations["TP6::LOC6"]["address"] = "500 MAIN ST"
    fake_supabase.permit_locations["TP6::LOC6"]["zip"] = "79401"
    # Property record with the SAME address+account exists only in
    # jurisdiction A -- jurisdiction B's detection run must not see it.
    seed_property(fake_supabase, "jur-a", "P6", "500 MAIN ST", "79401", acct="R800000")
    monkeypatch.setattr(
        matching.requests, "get",
        lambda *a, **kw: FakeAccountsResponse([
            {"record_id": "acc-6", "account_number": "R800000", "owner_name": "ACME HARDWARE LLC", "tax_year": 2026},
        ]),
    )

    result = new_business.run_new_business_detection("jur-b")
    assert result.existing_high_confidence == 0
    item = next(iter(fake_supabase.intelligence_items.values()))
    assert item["property_match_status"] == "NO_PROPERTY_MATCH"
