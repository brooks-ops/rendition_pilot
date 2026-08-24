from __future__ import annotations

import pytest

from app.comptroller import property_import
from app.comptroller.jurisdictions import Jurisdiction
from app.comptroller.property_import import PropertyImportError, import_property_csv, import_property_file
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
    stored = fake_supabase.real_property_records["jur-1::813538::None"]
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
    stored = fake_supabase.real_property_records["jur-1::813538::None"]
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
    assert fake_supabase.real_property_records["jur-1::813538::None"]["tug"] == "99"


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
    assert "jur-lubbock::813538::None" in fake_supabase.real_property_records
    assert "jur-other::PK-1::None" in fake_supabase.real_property_records
    assert len(fake_supabase.real_property_records) == 3


def test_reimporting_a_new_tax_year_preserves_the_prior_year(fake_supabase):
    jurisdiction = make_jurisdiction(property_field_mapping={**LUBBOCK_MAPPING, "tax_year": "AdHocTaxYear"})
    csv_2025 = (
        "PropertyID,QuickRefID,SitusAddress,SitusZip,TUG,NBHD,MapID,AdHocTaxYear\n"
        "813538,R163313,5807 88TH PL,79424,12,4400,R-33,2025\n"
    )
    csv_2026 = (
        "PropertyID,QuickRefID,SitusAddress,SitusZip,TUG,NBHD,MapID,AdHocTaxYear\n"
        "813538,R163313,5807 88TH PL,79424,14,4400,R-33,2026\n"
    )
    import_property_csv(jurisdiction, csv_2025)
    import_property_csv(jurisdiction, csv_2026)

    assert fake_supabase.real_property_records["jur-1::813538::2025"]["tug"] == "12"
    assert fake_supabase.real_property_records["jur-1::813538::2026"]["tug"] == "14"
    assert len(fake_supabase.real_property_records) == 2


def test_import_property_file_dispatches_csv_by_extension(fake_supabase):
    jurisdiction = make_jurisdiction()
    result = import_property_file(jurisdiction, CSV_TEXT.encode("utf-8"), "lubbock_export.csv")
    assert result.rows_imported == 2


def test_import_property_file_dispatches_excel_by_extension(fake_supabase):
    pd = pytest.importorskip("pandas")
    from io import BytesIO

    jurisdiction = make_jurisdiction()
    frame = pd.DataFrame([
        {"PropertyID": "813538", "QuickRefID": "R163313", "SitusAddress": "5807 88TH PL", "SitusZip": "79424", "TUG": "12", "NBHD": "4400", "MapID": "R-33"},
    ])
    buffer = BytesIO()
    frame.to_excel(buffer, index=False)

    result = import_property_file(jurisdiction, buffer.getvalue(), "lubbock_export.xlsx")

    assert result.rows_imported == 1
    assert fake_supabase.real_property_records["jur-1::813538::None"]["real_account_number"] == "R163313"


def test_import_property_file_rejects_unsupported_extension(fake_supabase):
    jurisdiction = make_jurisdiction()
    with pytest.raises(PropertyImportError):
        import_property_file(jurisdiction, b"not a real file", "export.pdf")


def test_transient_network_error_is_retried_and_recovers(fake_supabase, monkeypatch):
    """Regression test for a third real issue found importing the full
    240k-row Lubbock export: one batch hit a mid-stream SSL error over
    several hundred sequential upsert calls. The upsert is idempotent by
    key, so a transient failure must retry rather than aborting the whole
    import."""

    import requests

    from app.comptroller import property_import as pi

    monkeypatch.setattr(pi, "_UPSERT_RETRY_BACKOFF_SECONDS", 0)  # don't actually sleep in tests
    upsert_calls = {"n": 0}

    def flaky(method, url, *args, **kwargs):
        if url.endswith("real_property_records"):
            upsert_calls["n"] += 1
            if upsert_calls["n"] == 1:
                raise requests.exceptions.SSLError("sslv3 alert bad record mac")
        return fake_supabase.request_json(method, url, *args, **kwargs)

    monkeypatch.setattr(pi, "_request_json", flaky)
    jurisdiction = make_jurisdiction()

    result = import_property_csv(jurisdiction, CSV_TEXT)

    assert result.rows_imported == 2
    assert upsert_calls["n"] == 2  # one failed attempt, one successful retry


def test_transient_network_error_gives_up_after_max_attempts(fake_supabase, monkeypatch):
    import requests

    from app.comptroller import property_import as pi

    monkeypatch.setattr(pi, "_UPSERT_RETRY_BACKOFF_SECONDS", 0)

    def always_fails(*args, **kwargs):
        raise requests.exceptions.ConnectionError("connection reset")

    # _create_import_record must still succeed (uses the real fake) --
    # only the property-row upsert itself is permanently broken here.
    monkeypatch.setattr(pi, "_request_json", lambda method, url, *a, **kw: (
        always_fails() if url.endswith("real_property_records") else fake_supabase.request_json(method, url, *a, **kw)
    ))

    jurisdiction = make_jurisdiction()
    with pytest.raises(PropertyImportError, match="failed after"):
        import_property_csv(jurisdiction, CSV_TEXT)


def test_duplicate_property_id_within_one_file_collapses_instead_of_crashing(fake_supabase):
    """Regression test for a second real production bug found on the same
    real Lubbock import: ~11k rows share a PropertyID+tax_year (the same
    parcel/QuickRefID enumerated across many individually-addressed platted
    lots). Sending two rows with the same conflict key in one upsert
    statement raises 'ON CONFLICT DO UPDATE command cannot affect row a
    second time' in real Postgres. The last occurrence in the file must
    win, reported via rows_deduplicated, never silently dropped or crashed."""

    csv_with_duplicate_property_id = (
        "PropertyID,QuickRefID,SitusAddress,SitusZip,TUG,NBHD,MapID\n"
        "1000389,R342389,10005 UPLAND AVE,79424,198601,0728,101\n"
        "1000389,R342389,10007 UPLAND AVE,79424,198601,0728,101\n"
        "1000389,R342389,10009 UPLAND AVE,79424,198601,0728,101\n"
        "813538,R163313,5807 88TH PL,79424,198601,0718,107\n"
    )
    jurisdiction = make_jurisdiction()
    result = import_property_csv(jurisdiction, csv_with_duplicate_property_id)

    assert result.rows_read == 4
    assert result.rows_imported == 2  # 1000389 (collapsed) + 813538
    assert result.rows_deduplicated == 2  # two of the three 1000389 rows collapsed
    stored = fake_supabase.real_property_records["jur-1::1000389::None"]
    assert stored["situs_address_raw"] == "10009 UPLAND AVE"  # last one in the file wins


def test_upsert_targets_the_real_non_partial_unique_index(fake_supabase):
    """Regression test for a real production bug: PostgREST's on_conflict
    only matches a NON-partial unique index/constraint on exactly the
    column list given. The first real Lubbock import (240k rows, 100% with
    a tax_year) failed instantly with 'there is no unique or exclusion
    constraint matching the ON CONFLICT specification' because the original
    schema only had two PARTIAL indexes, which PostgREST's column-list-only
    on_conflict can never target (see
    supabase/migrations/20260825_fix_real_property_records_upsert_constraint.sql).
    This asserts the real request shape, not just that the fake accepts it --
    the fake would happily upsert with any on_conflict value."""

    jurisdiction = make_jurisdiction()
    import_property_csv(jurisdiction, CSV_TEXT)
    upsert_calls = [c for c in fake_supabase.calls if c["url"].endswith("real_property_records") and c["method"] == "POST"]
    assert upsert_calls, "expected at least one upsert POST to real_property_records"
    for call in upsert_calls:
        assert call["params"]["on_conflict"] == "jurisdiction_id,source_property_id,tax_year"
