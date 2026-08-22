from __future__ import annotations

import pytest

from app.comptroller import property_import
from app.comptroller.jurisdictions import Jurisdiction
from app.comptroller.property_import import PropertyImportError, import_property_csv
from tests.comptroller_fakes import FakeSupabase

LUBBOCK_MAPPING = {
    "source_property_id": "PropertyID", "real_account_number": "QuickRefID",
    "situs_address": "SitusAddress", "situs_zip": "SitusZip",
    "tug": "TUG", "neighborhood": "NBHD", "map_id": "MapID",
}

CSV_TEXT = (
    "PropertyID,QuickRefID,SitusAddress,SitusZip,TUG,NBHD,MapID\n"
    "813538,R163313,5807 88TH PL,79424,12,4400,R-33\n"
    "900001,R900001,100 MAIN ST,79401,7,1000,A-1\n"
    ",R000000,NO ID ROW,79401,1,1,A-2\n"  # missing PropertyID -- must be skipped
)


@pytest.fixture
def fake_supabase(monkeypatch):
    fake = FakeSupabase()
    monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "fake-service-role-key")
    monkeypatch.setattr(property_import, "_request_json", fake.request_json)
    return fake


def make_jurisdiction(**overrides) -> Jurisdiction:
    defaults = dict(
        id="jur-1", district_id="district-1", name="Lubbock CAD", slug="lubbock",
        county_name="Lubbock", state="TX", timezone="America/Chicago", active=True,
        comptroller_county_code="152", comptroller_dataset_id="3kx8-uryv",
        capabilities={"real_property_linkage": True}, cad_field_mapping={},
        property_field_mapping=LUBBOCK_MAPPING,
    )
    defaults.update(overrides)
    return Jurisdiction(**defaults)


def test_imports_rows_and_skips_missing_id(fake_supabase):
    jurisdiction = make_jurisdiction()
    result = import_property_csv(jurisdiction, CSV_TEXT, source_as_of_date="2026-08-01")
    assert result.rows_read == 3
    assert result.rows_imported == 2
    assert result.rows_skipped == 1
    stored = fake_supabase.real_property_records["jur-1::813538"]
    assert stored["real_account_number"] == "R163313"
    assert stored["situs_address_normalized"] == "5807 88TH PLACE"


def test_creates_an_import_version_record(fake_supabase):
    jurisdiction = make_jurisdiction()
    result = import_property_csv(jurisdiction, CSV_TEXT, source_as_of_date="2026-08-01", notes="test batch")
    assert result.import_id is not None
    import_row = fake_supabase.property_source_imports[result.import_id]
    assert import_row["row_count"] == 2
    assert import_row["source_as_of_date"] == "2026-08-01"
    assert import_row["notes"] == "test batch"
    stored = fake_supabase.real_property_records["jur-1::813538"]
    assert stored["source_import_id"] == result.import_id


def test_dry_run_writes_nothing(fake_supabase):
    jurisdiction = make_jurisdiction()
    result = import_property_csv(jurisdiction, CSV_TEXT, dry_run=True)
    assert result.rows_imported == 2
    assert fake_supabase.real_property_records == {}
    assert fake_supabase.property_source_imports == {}


def test_reimporting_same_property_id_updates_in_place(fake_supabase):
    jurisdiction = make_jurisdiction()
    import_property_csv(jurisdiction, CSV_TEXT)
    updated_csv = CSV_TEXT.replace("12,4400,R-33", "99,4400,R-33")
    import_property_csv(jurisdiction, updated_csv)
    assert len(fake_supabase.real_property_records) == 2  # not duplicated
    assert fake_supabase.real_property_records["jur-1::813538"]["tug"] == "99"


def test_raises_when_required_mapping_missing(fake_supabase):
    jurisdiction = make_jurisdiction(property_field_mapping={"real_account_number": "QuickRefID"})
    with pytest.raises(PropertyImportError):
        import_property_csv(jurisdiction, CSV_TEXT)


def test_jurisdiction_isolation_two_counties_do_not_collide(fake_supabase):
    lubbock = make_jurisdiction(id="jur-lubbock")
    other = make_jurisdiction(id="jur-other", property_field_mapping={
        "source_property_id": "ParcelKey", "real_account_number": "AccountRef", "situs_address": "PhysicalAddress",
    })
    import_property_csv(lubbock, CSV_TEXT)
    other_csv = "ParcelKey,AccountRef,PhysicalAddress\nPK-1,AC-1,42 County Road 100\n"
    import_property_csv(other, other_csv)
    assert "jur-lubbock::813538" in fake_supabase.real_property_records
    assert "jur-other::PK-1" in fake_supabase.real_property_records
    assert len(fake_supabase.real_property_records) == 3
