from __future__ import annotations

import pytest

from app.comptroller import property_adapter
from app.comptroller.jurisdictions import Jurisdiction
from app.comptroller.property_adapter import get_property_adapter, normalize_source_record
from tests.comptroller_fakes import FakeSupabase


@pytest.fixture
def fake_supabase(monkeypatch):
    fake = FakeSupabase()
    monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "fake-service-role-key")
    monkeypatch.setattr(property_adapter, "_request_json", fake.request_json)
    return fake


def make_jurisdiction(**overrides) -> Jurisdiction:
    defaults = dict(
        id="jur-1", district_id="district-1", name="Test CAD", slug="test",
        county_name="Test", state="TX", timezone="America/Chicago", active=True,
        comptroller_county_code="999", comptroller_dataset_id="3kx8-uryv",
        capabilities={"real_property_linkage": True},
        cad_field_mapping={},
        property_field_mapping={
            "source_property_id": "PropertyID", "real_account_number": "QuickRefID",
            "situs_address": "SitusAddress", "situs_zip": "SitusZip",
            "tug": "TUG", "neighborhood": "NBHD", "map_id": "MapID",
        },
    )
    defaults.update(overrides)
    return Jurisdiction(**defaults)


def test_normalize_source_record_maps_lubbock_shaped_columns():
    jurisdiction = make_jurisdiction()
    raw = {
        "PropertyID": "813538", "QuickRefID": "R163313", "SitusAddress": "5807 88TH PL",
        "SitusZip": "79424", "TUG": "12", "NBHD": "4400", "MapID": "R-33",
    }
    normalized = normalize_source_record(raw, jurisdiction.property_field_mapping, jurisdiction_id=jurisdiction.id)
    assert normalized["source_property_id"] == "813538"
    assert normalized["real_account_number"] == "R163313"
    assert normalized["situs_address_raw"] == "5807 88TH PL"
    assert normalized["situs_address_normalized"] == "5807 88TH PLACE"
    assert normalized["tug"] == "12"
    assert normalized["neighborhood"] == "4400"
    assert normalized["map_id"] == "R-33"
    assert normalized["jurisdiction_id"] == "jur-1"


def test_normalize_source_record_skips_row_with_no_property_id():
    jurisdiction = make_jurisdiction()
    raw = {"SitusAddress": "100 Main St"}
    assert normalize_source_record(raw, jurisdiction.property_field_mapping, jurisdiction_id=jurisdiction.id) is None


def test_normalize_source_record_never_reads_unmapped_columns():
    jurisdiction = make_jurisdiction(property_field_mapping={"source_property_id": "PropertyID", "situs_address": "SitusAddress"})
    raw = {"PropertyID": "1", "SitusAddress": "100 Main St", "QuickRefID": "SHOULD_NOT_BE_READ"}
    normalized = normalize_source_record(raw, jurisdiction.property_field_mapping, jurisdiction_id=jurisdiction.id)
    assert normalized["real_account_number"] is None


def test_get_property_by_id(fake_supabase):
    jurisdiction = make_jurisdiction()
    fake_supabase.real_property_records["jur-1::813538"] = {
        "id": "row-1", "jurisdiction_id": "jur-1", "source_property_id": "813538",
        "real_account_number": "R163313", "situs_address_raw": "5807 88TH PL",
        "situs_address_normalized": "5807 88TH PLACE", "situs_city": "LUBBOCK",
        "situs_state": "TX", "situs_zip": "79424", "owner_name": None,
        "tug": "12", "neighborhood": "4400", "map_id": "R-33",
        "latitude": None, "longitude": None, "source_system": "imported_file",
        "source_import_id": None, "source_updated_at": None,
    }
    adapter = get_property_adapter(jurisdiction)
    found = adapter.get_property_by_id(jurisdiction, "row-1")
    assert found is not None
    assert found.real_account_number == "R163313"
    assert adapter.get_property_by_id(jurisdiction, "missing") is None


def test_find_properties_by_address_scoped_to_jurisdiction(fake_supabase):
    jurisdiction_a = make_jurisdiction(id="jur-a")
    for jid, pid in [("jur-a", "P1"), ("jur-b", "P2")]:
        fake_supabase.real_property_records[f"{jid}::{pid}"] = {
            "id": f"row-{pid}", "jurisdiction_id": jid, "source_property_id": pid,
            "real_account_number": None, "situs_address_raw": "100 MAIN ST",
            "situs_address_normalized": "100 MAIN STREET", "situs_city": None,
            "situs_state": None, "situs_zip": None, "owner_name": None,
            "tug": None, "neighborhood": None, "map_id": None,
            "latitude": None, "longitude": None, "source_system": "imported_file",
            "source_import_id": None, "source_updated_at": None,
        }
    adapter = get_property_adapter(jurisdiction_a)
    results = adapter.find_properties_by_address(jurisdiction_a, "100 MAIN STREET")
    assert [r.property_id for r in results] == ["row-P1"]


def test_find_properties_by_real_account(fake_supabase):
    jurisdiction = make_jurisdiction()
    fake_supabase.real_property_records["jur-1::P1"] = {
        "id": "row-1", "jurisdiction_id": "jur-1", "source_property_id": "P1",
        "real_account_number": "R163313", "situs_address_raw": None,
        "situs_address_normalized": None, "situs_city": None, "situs_state": None,
        "situs_zip": None, "owner_name": None, "tug": None, "neighborhood": None,
        "map_id": None, "latitude": None, "longitude": None,
        "source_system": "imported_file", "source_import_id": None, "source_updated_at": None,
    }
    adapter = get_property_adapter(jurisdiction)
    results = adapter.find_properties_by_real_account(jurisdiction, "R163313")
    assert len(results) == 1
    assert adapter.find_properties_by_real_account(jurisdiction, "R999999") == []
