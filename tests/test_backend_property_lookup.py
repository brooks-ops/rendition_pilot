"""Authorization + wiring tests for the standalone Property Lookup endpoint
-- verifies district-admin gating, jurisdiction resolution, and graceful
handling of an unconfigured jurisdiction, not just a 200 response."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.comptroller import jurisdictions as comptroller_jurisdictions
from app.comptroller import property_adapter as comptroller_property_adapter
from app.comptroller import property_enrichment as comptroller_property_enrichment
from app.comptroller.address_normalizer import normalize_address
from app.comptroller.jurisdictions import Jurisdiction
from app.comptroller.property_adapter import NormalizedRealProperty
from app.comptroller.property_enrichment import PropertyEnrichmentError, PropertyEnrichmentOutcome
from app.comptroller.property_matching import PropertyMatchResult
from app.district_service import DistrictContext
from backend.main import PropertyLookupRequest, property_lookup


def make_district(district_id="district-1", role="admin"):
    return DistrictContext(
        district_id=district_id, district_slug="lubbock-cad", district_name="Lubbock CAD",
        email="admin@lubbockcad.org", user_id="user-1", role=role,
    )


def make_jurisdiction(**overrides) -> Jurisdiction:
    defaults = dict(
        id="jur-lubbock", district_id="district-1", name="Lubbock Central Appraisal District", slug="lubbock",
        county_name="Lubbock", state="TX", timezone="America/Chicago", active=True,
        comptroller_county_code="152", comptroller_dataset_id="3kx8-uryv",
        capabilities={"real_property_linkage": True}, cad_field_mapping={},
        property_field_mapping={"source_property_id": "PropertyID", "situs_address": "SitusAddress"},
    )
    defaults.update(overrides)
    return Jurisdiction(**defaults)


def test_requires_district_admin(monkeypatch):
    def deny(access_token):
        raise HTTPException(status_code=403, detail="nope")

    monkeypatch.setattr("backend.main.require_district_admin", deny)

    with pytest.raises(HTTPException) as exc_info:
        property_lookup(PropertyLookupRequest(access_token="fake", address="100 Main St"))
    assert exc_info.value.status_code == 403


def test_returns_404_when_no_jurisdiction_configured(monkeypatch):
    monkeypatch.setattr("backend.main.require_district_admin", lambda access_token: make_district())
    monkeypatch.setattr(comptroller_jurisdictions, "get_jurisdiction_by_district_id", lambda district_id: None)

    with pytest.raises(HTTPException) as exc_info:
        property_lookup(PropertyLookupRequest(access_token="fake", address="100 Main St"))
    assert exc_info.value.status_code == 404


def test_returns_400_when_capability_not_configured(monkeypatch):
    jurisdiction = make_jurisdiction(capabilities={})
    monkeypatch.setattr("backend.main.require_district_admin", lambda access_token: make_district())
    monkeypatch.setattr(comptroller_jurisdictions, "get_jurisdiction_by_district_id", lambda district_id: jurisdiction)
    monkeypatch.setattr(comptroller_property_adapter, "get_property_adapter", lambda j: type("A", (), {"search_properties": staticmethod(lambda j2: [])})())

    def fail(*a, **kw):
        raise PropertyEnrichmentError("Real Property Linkage is not enabled for Lubbock Central Appraisal District.")

    monkeypatch.setattr(comptroller_property_enrichment, "run_property_enrichment", fail)

    with pytest.raises(HTTPException) as exc_info:
        property_lookup(PropertyLookupRequest(access_token="fake", address="100 Main St"))
    assert exc_info.value.status_code == 400


def test_returns_match_details_for_own_jurisdiction(monkeypatch):
    jurisdiction = make_jurisdiction()
    monkeypatch.setattr("backend.main.require_district_admin", lambda access_token: make_district())
    monkeypatch.setattr(comptroller_jurisdictions, "get_jurisdiction_by_district_id", lambda district_id: jurisdiction)
    monkeypatch.setattr(comptroller_property_adapter, "get_property_adapter", lambda j: type("A", (), {"search_properties": staticmethod(lambda j2: [])})())

    matched = NormalizedRealProperty(
        property_id="row-1", jurisdiction_id="jur-lubbock", source_property_id="813538", tax_year=None,
        real_account_number="R163313", situs_address_raw="5807 88TH PL", situs_address_normalized="5807 88TH PLACE",
        situs_city=None, situs_state=None, situs_zip="79424", owner_name=None, tug="12", neighborhood="4400",
        map_id="R-33", latitude=None, longitude=None, source_system=None, source_import_id=None, source_updated_at=None,
    )
    outcome = PropertyEnrichmentOutcome(
        result=PropertyMatchResult(
            classification="EXACT_PROPERTY_MATCH", confidence="HIGH", score=1.0, matched_property=matched,
            candidate_count=1, reasons=["street number matched"], signals={"street_number": "MATCH"},
            normalized_input=normalize_address("5807 88th Pl", zip_code="79424"),
        ),
        from_cache=False, stored_row_id="er-1",
    )
    monkeypatch.setattr(comptroller_property_enrichment, "run_property_enrichment", lambda *a, **kw: outcome)

    response = property_lookup(PropertyLookupRequest(access_token="fake", address="5807 88th Pl", zip="79424"))

    assert response["classification"] == "EXACT_PROPERTY_MATCH"
    assert response["matched_property"]["property_id"] == "813538"
    assert response["matched_property"]["real_account_number"] == "R163313"
    assert response["matched_property"]["tug"] == "12"


def test_address_lookup_never_loads_the_full_property_table(monkeypatch):
    """Regression test for a real production bug: this endpoint used to
    prefetch adapter.search_properties() (every property in the
    jurisdiction) before every lookup. Against Lubbock's real 234,059-row
    table that took over a minute per request and made the tool look hung."""

    jurisdiction = make_jurisdiction()
    monkeypatch.setattr("backend.main.require_district_admin", lambda access_token: make_district())
    monkeypatch.setattr(comptroller_jurisdictions, "get_jurisdiction_by_district_id", lambda district_id: jurisdiction)

    def fail_if_called(j):
        raise AssertionError("address lookup must not prefetch the full property table")

    monkeypatch.setattr(comptroller_property_adapter, "get_property_adapter", fail_if_called)

    captured = {}

    def fake_run(jurisdiction, **kwargs):
        captured.update(kwargs)
        return PropertyEnrichmentOutcome(
            result=PropertyMatchResult(
                classification="NO_PROPERTY_MATCH", confidence="NONE", score=0.0, matched_property=None,
                candidate_count=0, reasons=[], signals={},
            ),
            from_cache=False, stored_row_id=None,
        )

    monkeypatch.setattr(comptroller_property_enrichment, "run_property_enrichment", fake_run)

    property_lookup(PropertyLookupRequest(access_token="fake", address="100 Main St"))

    assert "candidates" not in captured  # never passed a pre-fetched candidate list


def test_requires_address_or_account_number(monkeypatch):
    jurisdiction = make_jurisdiction()
    monkeypatch.setattr("backend.main.require_district_admin", lambda access_token: make_district())
    monkeypatch.setattr(comptroller_jurisdictions, "get_jurisdiction_by_district_id", lambda district_id: jurisdiction)

    with pytest.raises(HTTPException) as exc_info:
        property_lookup(PropertyLookupRequest(access_token="fake"))
    assert exc_info.value.status_code == 400


def test_looks_up_by_account_number(monkeypatch):
    jurisdiction = make_jurisdiction()
    monkeypatch.setattr("backend.main.require_district_admin", lambda access_token: make_district())
    monkeypatch.setattr(comptroller_jurisdictions, "get_jurisdiction_by_district_id", lambda district_id: jurisdiction)

    def fail_if_called(j):
        raise AssertionError("account-number lookup must not touch the property adapter/full table scan")

    monkeypatch.setattr(comptroller_property_adapter, "get_property_adapter", fail_if_called)

    matched = NormalizedRealProperty(
        property_id="row-1", jurisdiction_id="jur-lubbock", source_property_id="813538", tax_year=None,
        real_account_number="R163313", situs_address_raw="5807 88TH PL", situs_address_normalized="5807 88TH PLACE",
        situs_city=None, situs_state=None, situs_zip="79424", owner_name=None, tug=None, neighborhood=None,
        map_id=None, latitude=None, longitude=None, source_system=None, source_import_id=None, source_updated_at=None,
    )
    captured = {}

    def fake_lookup(jurisdiction, account_number):
        captured["account_number"] = account_number
        return PropertyMatchResult(
            classification="EXACT_PROPERTY_MATCH", confidence="HIGH", score=1.0, matched_property=matched,
            candidate_count=1, reasons=["Exact account number match."], signals={},
        )

    monkeypatch.setattr(comptroller_property_enrichment, "lookup_property_by_account_number", fake_lookup)

    response = property_lookup(PropertyLookupRequest(access_token="fake", account_number="R163313"))

    assert captured["account_number"] == "R163313"
    assert response["classification"] == "EXACT_PROPERTY_MATCH"
    assert response["matched_property"]["property_id"] == "813538"
