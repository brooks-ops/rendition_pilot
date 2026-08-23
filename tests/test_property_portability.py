"""Mandatory portability proof (spec item 29): the normalized property model
and ImportedPropertyAdapter must work for a jurisdiction whose raw property
export uses completely different column names than Lubbock's, with zero
changes to shared code -- only a different `property_field_mapping`.
"""

from __future__ import annotations

import pytest

from app.comptroller import property_adapter
from app.comptroller.jurisdictions import CAPABILITY_FIELD_REQUIREMENTS, Jurisdiction, validate_capability
from app.comptroller.property_adapter import get_property_adapter, normalize_source_record
from tests.comptroller_fakes import FakeSupabase

LUBBOCK_MAPPING = {
    "source_property_id": "PropertyID", "real_account_number": "QuickRefID",
    "situs_address": "SitusAddress", "situs_zip": "SitusZip",
    "tug": "TUG", "neighborhood": "NBHD", "map_id": "MapID",
}

# A hypothetical second Texas appraisal district's export -- deliberately
# unrelated column names, per spec item 29's example.
OTHER_COUNTY_MAPPING = {
    "source_property_id": "ParcelKey", "real_account_number": "AccountRef",
    "situs_address": "PhysicalAddress", "tax_year": "TaxYr",
    "tug": "TaxArea", "neighborhood": "NeighborhoodCode", "map_id": "MapNumber",
    # Deliberately omits situs_zip -- an optional field this county's export
    # doesn't carry, to also prove graceful degradation (spec item 17).
}

LUBBOCK_RAW_ROW = {
    "PropertyID": "813538", "QuickRefID": "R163313", "SitusAddress": "5807 88TH PL",
    "SitusZip": "79424", "TUG": "12", "NBHD": "4400", "MapID": "R-33",
}

OTHER_COUNTY_RAW_ROW = {
    "ParcelKey": "PK-5001", "AccountRef": "AC-9001", "PhysicalAddress": "42 County Road 100",
    "TaxArea": "TA-7", "NeighborhoodCode": "NC-3", "MapNumber": "MAP-88",
}


@pytest.fixture
def fake_supabase(monkeypatch):
    fake = FakeSupabase()
    monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "fake-service-role-key")
    monkeypatch.setattr(property_adapter, "_request_json", fake.request_json)
    return fake


def make_jurisdiction(slug, mapping, **overrides) -> Jurisdiction:
    defaults = dict(
        id=f"jur-{slug}", district_id=f"district-{slug}", name=f"{slug.title()} CAD", slug=slug,
        county_name=slug.title(), state="TX", timezone="America/Chicago", active=True,
        comptroller_county_code="1", comptroller_dataset_id="3kx8-uryv",
        capabilities={"real_property_linkage": True}, cad_field_mapping={},
        property_field_mapping=mapping,
    )
    defaults.update(overrides)
    return Jurisdiction(**defaults)


def test_two_differently_shaped_exports_normalize_into_the_same_shape():
    lubbock = make_jurisdiction("lubbock", LUBBOCK_MAPPING)
    other = make_jurisdiction("other-county", OTHER_COUNTY_MAPPING)

    lubbock_normalized = normalize_source_record(LUBBOCK_RAW_ROW, lubbock.property_field_mapping, jurisdiction_id=lubbock.id)
    other_normalized = normalize_source_record(OTHER_COUNTY_RAW_ROW, other.property_field_mapping, jurisdiction_id=other.id)

    # Same keys, same normalize_source_record() function, same
    # ImportedPropertyAdapter class -- only the mapping config differs.
    assert set(lubbock_normalized.keys()) == set(other_normalized.keys())

    assert lubbock_normalized["source_property_id"] == "813538"
    assert lubbock_normalized["real_account_number"] == "R163313"
    assert lubbock_normalized["tug"] == "12"

    assert other_normalized["source_property_id"] == "PK-5001"
    assert other_normalized["real_account_number"] == "AC-9001"
    assert other_normalized["situs_address_raw"] == "42 County Road 100"
    assert other_normalized["tug"] == "TA-7"
    assert other_normalized["neighborhood"] == "NC-3"
    assert other_normalized["map_id"] == "MAP-88"
    # Optional field the other county's export doesn't carry -- degrades to
    # None rather than raising or requiring code changes.
    assert other_normalized["situs_zip"] is None


def test_other_county_capability_validation_reports_only_missing_optional_zip():
    other = make_jurisdiction("other-county", OTHER_COUNTY_MAPPING)
    available = frozenset(other.property_field_mapping.keys())
    result = validate_capability(other, "real_property_linkage", available)
    assert result.ok is True
    assert result.missing_required == []
    assert result.missing_optional == ["situs_zip"]


def test_other_county_missing_required_field_blocks_cleanly():
    incomplete_mapping = {"real_account_number": "AccountRef"}  # no source_property_id/situs_address
    other = make_jurisdiction("other-county", incomplete_mapping)
    available = frozenset(other.property_field_mapping.keys())
    result = validate_capability(other, "real_property_linkage", available)
    assert result.ok is False
    assert set(result.missing_required) == set(CAPABILITY_FIELD_REQUIREMENTS["real_property_linkage"]["required"])
    assert "property source ID" in result.message
    assert "situs address" in result.message.lower()


def test_same_adapter_class_serves_both_jurisdictions(fake_supabase):
    lubbock = make_jurisdiction("lubbock", LUBBOCK_MAPPING)
    other = make_jurisdiction("other-county", OTHER_COUNTY_MAPPING)

    fake_supabase.real_property_records["jur-lubbock::813538"] = {
        "id": "row-lubbock", "jurisdiction_id": "jur-lubbock", "source_property_id": "813538",
        "real_account_number": "R163313", "situs_address_raw": "5807 88TH PL",
        "situs_address_normalized": "5807 88TH PLACE", "situs_city": None, "situs_state": None,
        "situs_zip": "79424", "owner_name": None, "tug": "12", "neighborhood": "4400", "map_id": "R-33",
        "latitude": None, "longitude": None, "source_system": "imported_file",
        "source_import_id": None, "source_updated_at": None,
    }
    fake_supabase.real_property_records["jur-other-county::PK-5001"] = {
        "id": "row-other", "jurisdiction_id": "jur-other-county", "source_property_id": "PK-5001",
        "real_account_number": "AC-9001", "situs_address_raw": "42 County Road 100",
        "situs_address_normalized": "42 COUNTY ROAD 100", "situs_city": None, "situs_state": None,
        "situs_zip": None, "owner_name": None, "tug": "TA-7", "neighborhood": "NC-3", "map_id": "MAP-88",
        "latitude": None, "longitude": None, "source_system": "imported_file",
        "source_import_id": None, "source_updated_at": None,
    }

    lubbock_adapter = get_property_adapter(lubbock)
    other_adapter = get_property_adapter(other)
    assert type(lubbock_adapter) is type(other_adapter)

    lubbock_result = lubbock_adapter.get_property_by_id(lubbock, "row-lubbock")
    other_result = other_adapter.get_property_by_id(other, "row-other")
    assert lubbock_result.real_account_number == "R163313"
    assert other_result.real_account_number == "AC-9001"

    # Jurisdiction isolation: Lubbock's adapter call never sees the other
    # county's row even though both live in the same fake table.
    assert lubbock_adapter.get_property_by_id(lubbock, "row-other") is None
