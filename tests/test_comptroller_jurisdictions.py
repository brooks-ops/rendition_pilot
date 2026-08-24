from __future__ import annotations

import pytest

from app.comptroller import jurisdictions as j
from tests.comptroller_fakes import FakeSupabase


@pytest.fixture
def fake_supabase(monkeypatch):
    fake = FakeSupabase()
    monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "fake-service-role-key")
    monkeypatch.setattr(j, "_request_json", fake.request_json)
    return fake


def seed_lubbock(fake_supabase, **overrides):
    row = {
        "id": "jur-lubbock",
        "district_id": "district-lubbock",
        "name": "Lubbock Central Appraisal District",
        "slug": "lubbock",
        "county_name": "Lubbock",
        "state": "TX",
        "timezone": "America/Chicago",
        "active": True,
        "comptroller_county_code": "152",
        "comptroller_dataset_id": "3kx8-uryv",
        "capabilities": {"sales_tax_monitoring": True, "new_business_detection": True},
        "cad_field_mapping": {},
    }
    row.update(overrides)
    fake_supabase.jurisdictions[row["id"]] = row
    return row


def test_get_jurisdiction_by_id(fake_supabase):
    seed_lubbock(fake_supabase)
    jurisdiction = j.get_jurisdiction("jur-lubbock")
    assert jurisdiction.name == "Lubbock Central Appraisal District"
    assert jurisdiction.comptroller_county_code == "152"
    assert jurisdiction.has_capability("new_business_detection")
    assert not jurisdiction.has_capability("dba_monitoring")


def test_get_jurisdiction_by_slug(fake_supabase):
    seed_lubbock(fake_supabase)
    jurisdiction = j.get_jurisdiction_by_slug("lubbock")
    assert jurisdiction.id == "jur-lubbock"


def test_get_jurisdiction_by_slug_not_found_raises(fake_supabase):
    with pytest.raises(j.JurisdictionError):
        j.get_jurisdiction_by_slug("nonexistent")


def test_list_active_jurisdictions_filters_inactive(fake_supabase):
    seed_lubbock(fake_supabase)
    seed_lubbock(fake_supabase, id="jur-inactive", slug="inactive-county", active=False)

    active = j.list_active_jurisdictions()

    assert len(active) == 1
    assert active[0].slug == "lubbock"


def test_list_active_jurisdictions_filters_by_capability(fake_supabase):
    seed_lubbock(fake_supabase)
    seed_lubbock(
        fake_supabase,
        id="jur-no-nbd",
        slug="no-nbd-county",
        capabilities={"sales_tax_monitoring": True, "new_business_detection": False},
    )

    with_nbd = j.list_active_jurisdictions(capability="new_business_detection")

    assert [x.slug for x in with_nbd] == ["lubbock"]


# -- validate_capability ------------------------------------------------------


def test_validate_capability_not_enabled(fake_supabase):
    jurisdiction = j._row_to_jurisdiction(seed_lubbock(fake_supabase, capabilities={"new_business_detection": False}))
    result = j.validate_capability(jurisdiction, "new_business_detection", frozenset({"owner_name"}))
    assert result.ok is False
    assert "not enabled" in result.message


def test_validate_capability_missing_county_code(fake_supabase):
    jurisdiction = j._row_to_jurisdiction(seed_lubbock(fake_supabase, comptroller_county_code=None))
    result = j.validate_capability(jurisdiction, "new_business_detection", frozenset({"owner_name"}))
    assert result.ok is False
    assert "comptroller_county_code" in result.missing_required
    assert "cannot run" in result.message.lower()


def test_validate_capability_missing_required_field(fake_supabase):
    jurisdiction = j._row_to_jurisdiction(seed_lubbock(fake_supabase))
    result = j.validate_capability(jurisdiction, "new_business_detection", frozenset())  # no owner_name available
    assert result.ok is False
    assert "owner_name" in result.missing_required
    assert "cannot run" in result.message.lower()


def test_validate_capability_available_with_reduced_matching(fake_supabase):
    jurisdiction = j._row_to_jurisdiction(seed_lubbock(fake_supabase))
    result = j.validate_capability(jurisdiction, "new_business_detection", frozenset({"owner_name", "account_number"}))
    assert result.ok is True
    assert "situs_address" in result.missing_optional
    assert "reduced matching capability" in result.message.lower()


def test_validate_capability_fully_available(fake_supabase):
    jurisdiction = j._row_to_jurisdiction(seed_lubbock(fake_supabase))
    all_fields = frozenset({"owner_name", "situs_address", "dba_name", "mailing_address", "property_type"})
    result = j.validate_capability(jurisdiction, "new_business_detection", all_fields)
    assert result.ok is True
    assert result.missing_optional == []
