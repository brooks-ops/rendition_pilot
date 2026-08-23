"""Authorization tests for the admin-only production readiness endpoint."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.comptroller import jurisdictions as comptroller_jurisdictions
from app.comptroller import readiness as comptroller_readiness
from app.comptroller.jurisdictions import Jurisdiction
from app.comptroller.readiness import ProductionReadiness, ReadinessCheck
from app.district_service import DistrictContext
from backend.main import ProductionReadinessRequest, production_readiness


def make_district(district_id="district-1", role="admin"):
    return DistrictContext(
        district_id=district_id, district_slug="lubbock-cad", district_name="Lubbock CAD",
        email="admin@lubbockcad.org", user_id="user-1", role=role,
    )


def make_jurisdiction(**overrides) -> Jurisdiction:
    defaults = dict(
        id="jur-lubbock", district_id="district-1", name="Lubbock CAD", slug="lubbock", county_name="Lubbock",
        state="TX", timezone="America/Chicago", active=True, comptroller_county_code="152",
        comptroller_dataset_id="3kx8-uryv", capabilities={}, cad_field_mapping={}, property_field_mapping={},
        appraiser_assignment_rules={},
    )
    defaults.update(overrides)
    return Jurisdiction(**defaults)


def test_requires_district_admin(monkeypatch):
    def deny(access_token):
        raise HTTPException(status_code=403, detail="nope")

    monkeypatch.setattr("backend.main.require_district_admin", deny)

    with pytest.raises(HTTPException) as exc_info:
        production_readiness(ProductionReadinessRequest(access_token="fake"))
    assert exc_info.value.status_code == 403


def test_returns_404_when_no_jurisdiction_configured(monkeypatch):
    monkeypatch.setattr("backend.main.require_district_admin", lambda access_token: make_district())
    monkeypatch.setattr(comptroller_jurisdictions, "get_jurisdiction_by_district_id", lambda district_id: None)

    with pytest.raises(HTTPException) as exc_info:
        production_readiness(ProductionReadinessRequest(access_token="fake"))
    assert exc_info.value.status_code == 404


def test_returns_checks_for_own_jurisdiction(monkeypatch):
    jurisdiction = make_jurisdiction()
    monkeypatch.setattr("backend.main.require_district_admin", lambda access_token: make_district())
    monkeypatch.setattr(comptroller_jurisdictions, "get_jurisdiction_by_district_id", lambda district_id: jurisdiction)
    fake_result = ProductionReadiness(
        jurisdiction_id="jur-lubbock", jurisdiction_name="Lubbock CAD",
        checks=[ReadinessCheck(name="Comptroller data", status="READY", detail="synced")],
    )
    monkeypatch.setattr(comptroller_readiness, "assess_production_readiness", lambda j: fake_result)

    response = production_readiness(ProductionReadinessRequest(access_token="fake"))

    assert response["jurisdiction_name"] == "Lubbock CAD"
    assert response["checks"][0]["name"] == "Comptroller data"
    assert response["checks"][0]["status"] == "READY"
