"""Tests for the persistence wiring added to POST /api/review/lock --
verifies a valid token persists under the SERVER-VERIFIED district (never
the client-supplied district_context), an invalid/missing token still lets
the lock succeed without persisting, and a persistence-layer failure never
blocks the appraiser's response."""

from __future__ import annotations

from fastapi import HTTPException

from app.district_service import DistrictContext
from app.rendition_persistence import PersistedRenditionResult, RenditionPersistenceError
from backend.main import LockReviewRequest, review_lock

RESULT = {"metadata": {"tax_year": "2026", "owner_name": "ACME HARDWARE LLC", "account_number": "P0001234"}}


def make_request(**overrides) -> LockReviewRequest:
    defaults = dict(
        file_name="rendition.pdf", result=RESULT, final_value=100.0, final_source="manual",
        appraiser_notes="", appraiser_initials="JS", account_number="P0001234", decision="accepted",
        district_context={"district_id": "CLIENT-SUPPLIED-SHOULD-NOT-BE-USED"},
        access_token=None,
    )
    defaults.update(overrides)
    return LockReviewRequest(**defaults)


def test_lock_persists_under_the_server_verified_district_not_the_client_supplied_one(monkeypatch):
    captured = {}

    def fake_persist(**kwargs):
        captured.update(kwargs)
        return PersistedRenditionResult(parsed_rendition_result_id="prr-1", upload_id="up-1", job_id="job-1", created=True)

    monkeypatch.setattr(
        "backend.main.get_authenticated_district_context",
        lambda access_token: DistrictContext(
            district_id="district-REAL", district_slug="lubbock-cad", district_name="Lubbock CAD",
            email="appraiser@lubbockcad.org", user_id="user-1", role="member",
        ),
    )
    monkeypatch.setattr("backend.main.get_supabase_user", lambda access_token: {"id": "user-1"})
    monkeypatch.setattr("backend.main.persist_locked_review", fake_persist)

    response = review_lock(make_request(access_token="valid-token"))

    assert response["persisted"] is True
    assert captured["district_id"] == "district-REAL"
    assert captured["district_id"] != "CLIENT-SUPPLIED-SHOULD-NOT-BE-USED"
    assert captured["account_number"] == "P0001234"
    assert captured["created_by"] == "user-1"


def test_lock_succeeds_without_persisting_when_no_access_token(monkeypatch):
    def fail_if_called(**kwargs):
        raise AssertionError("persist_locked_review must not be called without an access_token")

    monkeypatch.setattr("backend.main.persist_locked_review", fail_if_called)

    response = review_lock(make_request(access_token=None))

    assert response["persisted"] is False
    assert response["final_record"]["account_number"] == "P0001234"


def test_lock_succeeds_when_token_is_invalid(monkeypatch):
    def deny(access_token):
        raise HTTPException(status_code=403, detail="invalid token")

    monkeypatch.setattr("backend.main.get_authenticated_district_context", deny)

    response = review_lock(make_request(access_token="garbage-token"))

    assert response["persisted"] is False
    assert response["final_record"]["account_number"] == "P0001234"


def test_lock_succeeds_when_persistence_layer_raises(monkeypatch):
    monkeypatch.setattr(
        "backend.main.get_authenticated_district_context",
        lambda access_token: DistrictContext(
            district_id="district-1", district_slug="lubbock-cad", district_name="Lubbock CAD",
            email="a@lubbockcad.org", user_id="user-1", role="member",
        ),
    )
    monkeypatch.setattr("backend.main.get_supabase_user", lambda access_token: {"id": "user-1"})

    def fail(**kwargs):
        raise RenditionPersistenceError("boom")

    monkeypatch.setattr("backend.main.persist_locked_review", fail)

    response = review_lock(make_request(access_token="valid-token"))

    assert response["persisted"] is False
    assert response["final_record"]["account_number"] == "P0001234"
