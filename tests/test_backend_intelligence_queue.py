"""Authorization tests for the BPP Intelligence Queue endpoints -- verifies
gating (must be a district admin) and cross-district isolation, not just
that endpoints return 200. Mirrors tests/test_backend_review_save.py's
pattern of importing endpoint functions directly."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.comptroller import intelligence as comptroller_intelligence
from app.district_service import DistrictContext
from backend.main import (
    ComptrollerStatusRequest,
    IntelligenceDismissRequest,
    IntelligenceInvestigateRequest,
    IntelligenceItemRequest,
    IntelligenceQueueRequest,
    IntelligenceResolveRequest,
    intelligence_dismiss,
    intelligence_investigate,
    intelligence_item_detail,
    intelligence_queue,
    intelligence_resolve,
    intelligence_summary,
)


def make_district(district_id="district-1", role="admin"):
    return DistrictContext(
        district_id=district_id, district_slug="lubbock-cad", district_name="Lubbock CAD",
        email="admin@lubbockcad.org", user_id="user-1", role=role,
    )


def make_item(item_id="intel-1", district_id="district-1", **overrides):
    defaults = dict(
        id=item_id, source_table=comptroller_intelligence.SOURCE_TABLE_INTELLIGENCE, signal_type="new_business",
        status="NEW", classification="NO_ACCOUNT_FOUND", priority="HIGH", confidence="UNMATCHED",
        confidence_score=0.0, is_ambiguous=False, business_name="JOE'S SPORTS BAR", legal_name=None,
        source_address=None, source_city="LUBBOCK", source_state="TX", source_zip="79401",
        permit_start_date=None, permit_end_date=None, first_detected_at=None, matched_account_number=None,
        matched_owner_name=None, match_reason=None, match_signals=None, recommended_action=None,
        resolution=None, resolution_notes=None, reviewed_by=None, reviewed_at=None,
        district_id=district_id, jurisdiction_id="jur-lubbock", created_at="2026-08-15T00:00:00+00:00", raw={},
    )
    defaults.update(overrides)
    return comptroller_intelligence.UnifiedIntelligenceItem(**defaults)


@pytest.fixture(autouse=True)
def stub_auth(monkeypatch):
    monkeypatch.setattr("backend.main.require_district_admin", lambda access_token: make_district())
    monkeypatch.setattr("backend.main.get_supabase_user", lambda access_token: {"id": "user-1", "email": "admin@lubbockcad.org"})


def test_summary_requires_admin(monkeypatch):
    def deny(access_token):
        raise HTTPException(status_code=403, detail="Only district admins can manage authorized users.")

    monkeypatch.setattr("backend.main.require_district_admin", deny)
    monkeypatch.setattr(comptroller_intelligence, "get_queue_summary", lambda district_id: {"new": 0})

    with pytest.raises(HTTPException) as exc_info:
        intelligence_summary(ComptrollerStatusRequest(access_token="fake"))
    assert exc_info.value.status_code == 403


def test_summary_calls_module_with_requesters_district(monkeypatch):
    captured = {}

    def fake_get_queue_summary(district_id):
        captured["district_id"] = district_id
        return {"new": 1, "high_priority": 0, "needs_review": 0, "resolved": 0, "total": 1}

    monkeypatch.setattr(comptroller_intelligence, "get_queue_summary", fake_get_queue_summary)

    result = intelligence_summary(ComptrollerStatusRequest(access_token="fake"))

    assert captured["district_id"] == "district-1"
    assert result["new"] == 1


def test_queue_passes_filters_through(monkeypatch):
    captured = {}

    def fake_list_intelligence_queue(district_id, **kwargs):
        captured["kwargs"] = kwargs
        return []

    monkeypatch.setattr(comptroller_intelligence, "list_intelligence_queue", fake_list_intelligence_queue)

    intelligence_queue(IntelligenceQueueRequest(access_token="fake", signal_type="new_business", status="NEW"))

    assert captured["kwargs"]["signal_type"] == "new_business"
    assert captured["kwargs"]["status"] == "NEW"


def test_item_detail_blocks_cross_district_access(monkeypatch):
    other_district_item = make_item(district_id="district-OTHER")
    monkeypatch.setattr(comptroller_intelligence, "get_intelligence_item", lambda source_table, item_id: other_district_item)

    with pytest.raises(HTTPException) as exc_info:
        intelligence_item_detail(IntelligenceItemRequest(
            access_token="fake", source_table=comptroller_intelligence.SOURCE_TABLE_INTELLIGENCE, item_id="intel-1",
        ))
    assert exc_info.value.status_code == 403


def test_item_detail_404_when_missing(monkeypatch):
    monkeypatch.setattr(comptroller_intelligence, "get_intelligence_item", lambda source_table, item_id: None)

    with pytest.raises(HTTPException) as exc_info:
        intelligence_item_detail(IntelligenceItemRequest(
            access_token="fake", source_table=comptroller_intelligence.SOURCE_TABLE_INTELLIGENCE, item_id="missing",
        ))
    assert exc_info.value.status_code == 404


def test_item_detail_returns_own_district_item(monkeypatch):
    monkeypatch.setattr(comptroller_intelligence, "get_intelligence_item", lambda source_table, item_id: make_item())

    result = intelligence_item_detail(IntelligenceItemRequest(
        access_token="fake", source_table=comptroller_intelligence.SOURCE_TABLE_INTELLIGENCE, item_id="intel-1",
    ))

    assert result["item"]["id"] == "intel-1"


def test_investigate_blocks_cross_district(monkeypatch):
    monkeypatch.setattr(comptroller_intelligence, "get_intelligence_item", lambda source_table, item_id: make_item(district_id="district-OTHER"))

    with pytest.raises(HTTPException) as exc_info:
        intelligence_investigate(IntelligenceInvestigateRequest(
            access_token="fake", source_table=comptroller_intelligence.SOURCE_TABLE_INTELLIGENCE, item_id="intel-1",
        ))
    assert exc_info.value.status_code == 403


def test_resolve_blocks_cross_district(monkeypatch):
    monkeypatch.setattr(comptroller_intelligence, "get_intelligence_item", lambda source_table, item_id: make_item(district_id="district-OTHER"))

    with pytest.raises(HTTPException) as exc_info:
        intelligence_resolve(IntelligenceResolveRequest(
            access_token="fake", source_table=comptroller_intelligence.SOURCE_TABLE_INTELLIGENCE,
            item_id="intel-1", resolution="OTHER",
        ))
    assert exc_info.value.status_code == 403


def test_resolve_succeeds_for_own_district_item(monkeypatch):
    monkeypatch.setattr(comptroller_intelligence, "get_intelligence_item", lambda source_table, item_id: make_item())
    captured = {}

    def fake_resolve_item(source_table, item_id, **kwargs):
        captured["kwargs"] = kwargs
        return make_item(status="RESOLVED")

    monkeypatch.setattr(comptroller_intelligence, "resolve_item", fake_resolve_item)

    result = intelligence_resolve(IntelligenceResolveRequest(
        access_token="fake", source_table=comptroller_intelligence.SOURCE_TABLE_INTELLIGENCE,
        item_id="intel-1", resolution="NEW_ACCOUNT_NEEDED", resolution_notes="confirmed",
    ))

    assert captured["kwargs"]["resolution"] == "NEW_ACCOUNT_NEEDED"
    assert captured["kwargs"]["reviewed_by"] == "user-1"
    assert result["item"]["status"] == "RESOLVED"


def test_dismiss_blocks_cross_district(monkeypatch):
    monkeypatch.setattr(comptroller_intelligence, "get_intelligence_item", lambda source_table, item_id: make_item(district_id="district-OTHER"))

    with pytest.raises(HTTPException) as exc_info:
        intelligence_dismiss(IntelligenceDismissRequest(
            access_token="fake", source_table=comptroller_intelligence.SOURCE_TABLE_INTELLIGENCE, item_id="intel-1",
        ))
    assert exc_info.value.status_code == 403
