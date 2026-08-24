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


def _year_row(source_property_id, tax_year, tug, row_id=None):
    return {
        "id": row_id or f"row-{source_property_id}-{tax_year}", "jurisdiction_id": "jur-1",
        "source_property_id": source_property_id, "tax_year": tax_year,
        "real_account_number": "R1", "situs_address_raw": "100 MAIN ST",
        "situs_address_normalized": "100 MAIN STREET", "situs_city": None, "situs_state": None,
        "situs_zip": None, "owner_name": None, "tug": tug, "neighborhood": None, "map_id": None,
        "latitude": None, "longitude": None, "source_system": "imported_file",
        "source_import_id": None, "source_updated_at": None,
    }


def test_search_properties_prefers_jurisdictions_current_tax_year(fake_supabase):
    jurisdiction = make_jurisdiction(current_tax_year=2025)
    fake_supabase.real_property_records["a"] = _year_row("P1", 2024, "OLD")
    fake_supabase.real_property_records["b"] = _year_row("P1", 2025, "CURRENT")
    fake_supabase.real_property_records["c"] = _year_row("P1", 2026, "NEWER")
    adapter = get_property_adapter(jurisdiction)
    results = adapter.search_properties(jurisdiction)
    assert len(results) == 1
    assert results[0].tug == "CURRENT"


def test_search_properties_falls_back_to_newest_year_when_current_unset(fake_supabase):
    jurisdiction = make_jurisdiction()  # current_tax_year defaults to None
    fake_supabase.real_property_records["a"] = _year_row("P1", 2024, "OLD")
    fake_supabase.real_property_records["b"] = _year_row("P1", 2026, "NEWEST")
    adapter = get_property_adapter(jurisdiction)
    results = adapter.search_properties(jurisdiction)
    assert len(results) == 1
    assert results[0].tug == "NEWEST"


def test_search_properties_falls_back_to_newest_when_current_year_row_missing(fake_supabase):
    jurisdiction = make_jurisdiction(current_tax_year=2030)  # no 2030 row exists
    fake_supabase.real_property_records["a"] = _year_row("P1", 2024, "OLD")
    fake_supabase.real_property_records["b"] = _year_row("P1", 2026, "NEWEST")
    adapter = get_property_adapter(jurisdiction)
    results = adapter.search_properties(jurisdiction)
    assert results[0].tug == "NEWEST"


def test_search_properties_keeps_undated_row_when_no_dated_rows_exist(fake_supabase):
    jurisdiction = make_jurisdiction()
    fake_supabase.real_property_records["a"] = _year_row("P1", None, "UNDATED")
    adapter = get_property_adapter(jurisdiction)
    results = adapter.search_properties(jurisdiction)
    assert len(results) == 1
    assert results[0].tug == "UNDATED"


def test_search_properties_paginates_through_every_row(fake_supabase):
    jurisdiction = make_jurisdiction()
    for i in range(5):
        fake_supabase.real_property_records[f"row-{i}"] = _year_row(f"P{i}", None, "X", row_id=f"row-{i}")
    adapter = get_property_adapter(jurisdiction)
    results = adapter.search_properties(jurisdiction)  # limit=None -> pagination path
    assert len(results) == 5


def test_search_properties_paginates_past_a_server_side_row_cap(fake_supabase, monkeypatch):
    """Regression test for a real production bug: a live Supabase project
    silently caps every response at its own configured max-rows (found to
    be 1000) regardless of the `limit` requested. Pagination must advance by
    how many rows actually came back, not by the requested page_size, or it
    stops after exactly one capped page -- found importing the real
    234,059-row Lubbock property table (search_properties returned exactly
    1000 candidates instead of all of them)."""

    SERVER_CAP = 3
    real_handler = fake_supabase.request_json

    def capped(method, url, headers, *, params=None, json_payload=None):
        if url.endswith("real_property_records") and method == "GET" and params and int(params.get("limit", 0)) > SERVER_CAP:
            params = {**params, "limit": SERVER_CAP}
        return real_handler(method, url, headers, params=params, json_payload=json_payload)

    monkeypatch.setattr(property_adapter, "_request_json", capped)

    jurisdiction = make_jurisdiction()
    for i in range(10):
        fake_supabase.real_property_records[f"row-{i}"] = _year_row(f"P{i}", None, "X", row_id=f"row-{i}")

    adapter = get_property_adapter(jurisdiction)
    results = adapter.search_properties(jurisdiction, limit=None)  # requests page_size=2000, server caps at 3

    assert len(results) == 10


def test_search_properties_with_explicit_limit_uses_single_fetch(fake_supabase):
    jurisdiction = make_jurisdiction()
    for i in range(3):
        fake_supabase.real_property_records[f"row-{i}"] = _year_row(f"P{i}", None, "X", row_id=f"row-{i}")
    adapter = get_property_adapter(jurisdiction)
    results = adapter.search_properties(jurisdiction, limit=2)
    assert len(results) == 2
