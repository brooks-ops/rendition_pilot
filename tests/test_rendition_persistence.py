"""Tests for app/rendition_persistence.py -- the fix for the previously
100%-empty parsed_rendition_results table (see docs/rendition_persistence.md).

Covers the real gaps this pass closed: persistence itself, dedup/upsert
semantics, tax-year preservation, jurisdiction (district) isolation, and
that the exact shape written is what app.comptroller.matching's existing,
unmodified matcher actually expects to read (tested via the real
MatchCandidate/fetch_candidate_records path, not a hand-built stand-in).
"""

from __future__ import annotations

import pytest

from app import rendition_persistence
from app.comptroller import matching
from app.rendition_persistence import RenditionPersistenceError, persist_locked_review
from tests.comptroller_fakes import FakeSupabase


@pytest.fixture
def fake_supabase(monkeypatch):
    fake = FakeSupabase()
    monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "fake-service-role-key")
    monkeypatch.setattr(rendition_persistence, "_request_json", fake.request_json)
    return fake


PIPELINE_RESULT = {
    "metadata": {
        "tax_year": "2026",
        "owner_name": "ACME HARDWARE LLC",
        "account_number": "P0001234",  # OCR-extracted, possibly wrong/incomplete
        "signed_date": "01/15/2026",
    },
    "assessment_summary": {"confidence": 0.82, "recommended_path": "cost_approach"},
}


def test_persists_new_locked_review(fake_supabase):
    outcome = persist_locked_review(
        district_id="district-1", file_name="rendition.pdf", result=PIPELINE_RESULT,
        account_number="P0001234", final_value=125000.0, pipeline_confidence=0.82,
        created_by="user-1",
    )
    assert outcome.created is True
    stored = fake_supabase.parsed_rendition_results[outcome.parsed_rendition_result_id]
    assert stored["district_id"] == "district-1"
    assert stored["tax_year"] == 2026
    assert stored["recommended_value"] == 125000.0
    assert stored["confidence"] == 0.82
    assert stored["result"]["metadata"]["account_number"] == "P0001234"
    assert len(fake_supabase.rendition_uploads) == 1
    assert len(fake_supabase.rendition_jobs) == 1


def test_appraiser_confirmed_account_number_overrides_ocr_value(fake_supabase):
    """The appraiser typed a corrected account number at lock time that
    differs from what OCR extracted -- the confirmed value must win, since
    it's what any future matching should trust (spec item 21)."""

    outcome = persist_locked_review(
        district_id="district-1", file_name="rendition.pdf", result=PIPELINE_RESULT,
        account_number="P0009999", final_value=100.0,  # appraiser corrected the OCR guess
    )
    stored = fake_supabase.parsed_rendition_results[outcome.parsed_rendition_result_id]
    assert stored["result"]["metadata"]["account_number"] == "P0009999"
    # Everything else in the pipeline result is preserved, not discarded.
    assert stored["result"]["metadata"]["owner_name"] == "ACME HARDWARE LLC"
    assert stored["result"]["assessment_summary"]["recommended_path"] == "cost_approach"


def test_relocking_same_account_and_year_updates_in_place(fake_supabase):
    first = persist_locked_review(
        district_id="district-1", file_name="a.pdf", result=PIPELINE_RESULT,
        account_number="P0001234", final_value=100000.0,
    )
    second = persist_locked_review(
        district_id="district-1", file_name="a-corrected.pdf", result=PIPELINE_RESULT,
        account_number="P0001234", final_value=110000.0,
    )
    assert second.created is False
    assert second.parsed_rendition_result_id == first.parsed_rendition_result_id
    assert len(fake_supabase.parsed_rendition_results) == 1
    assert fake_supabase.parsed_rendition_results[first.parsed_rendition_result_id]["recommended_value"] == 110000.0


def test_different_tax_year_creates_a_separate_row(fake_supabase):
    result_2025 = {"metadata": {**PIPELINE_RESULT["metadata"], "tax_year": "2025"}}
    result_2026 = {"metadata": {**PIPELINE_RESULT["metadata"], "tax_year": "2026"}}
    persist_locked_review(district_id="district-1", file_name="a.pdf", result=result_2025, account_number="P0001234", final_value=1.0)
    persist_locked_review(district_id="district-1", file_name="b.pdf", result=result_2026, account_number="P0001234", final_value=2.0)
    assert len(fake_supabase.parsed_rendition_results) == 2


def test_different_districts_never_collide(fake_supabase):
    """Jurisdiction isolation: the same account number in two different
    districts must never merge into one row or leak across districts."""

    persist_locked_review(district_id="district-a", file_name="a.pdf", result=PIPELINE_RESULT, account_number="P0001234", final_value=1.0)
    persist_locked_review(district_id="district-b", file_name="b.pdf", result=PIPELINE_RESULT, account_number="P0001234", final_value=2.0)
    assert len(fake_supabase.parsed_rendition_results) == 2
    district_ids = {row["district_id"] for row in fake_supabase.parsed_rendition_results.values()}
    assert district_ids == {"district-a", "district-b"}


def test_missing_tax_year_never_dedups_and_never_crashes(fake_supabase):
    result_no_year = {"metadata": {"owner_name": "NO YEAR LLC", "account_number": "P0005555"}}
    first = persist_locked_review(district_id="district-1", file_name="a.pdf", result=result_no_year, account_number="P0005555", final_value=1.0)
    second = persist_locked_review(district_id="district-1", file_name="b.pdf", result=result_no_year, account_number="P0005555", final_value=2.0)
    assert first.created is True
    assert second.created is True  # can't dedup without a year -- documented, not a bug
    assert len(fake_supabase.parsed_rendition_results) == 2


def test_requires_account_number(fake_supabase):
    with pytest.raises(RenditionPersistenceError):
        persist_locked_review(district_id="district-1", file_name="a.pdf", result=PIPELINE_RESULT, account_number="", final_value=1.0)


def test_non_numeric_confidence_stored_as_none(fake_supabase):
    outcome = persist_locked_review(
        district_id="district-1", file_name="a.pdf", result=PIPELINE_RESULT,
        account_number="P0001234", final_value=1.0, pipeline_confidence="HIGH",  # not numeric
    )
    stored = fake_supabase.parsed_rendition_results[outcome.parsed_rendition_result_id]
    assert stored["confidence"] is None


class FakeHttpResponse:
    def __init__(self, rows):
        self.status_code = 200
        self._rows = rows
        self.text = ""

    def json(self):
        return self._rows


def _postgrest_select_shape(row: dict) -> dict:
    """Manually reproduces what a real PostgREST `select=record_id:id,
    account_number:result->metadata->>account_number,
    owner_name:result->metadata->>owner_name,tax_year:tax_year` response
    would look like for one row. app.comptroller.matching.fetch_candidate_records
    calls `requests.get` directly (not the shared `_request_json`/FakeSupabase
    path everything else in app.comptroller.* uses), so there's no generic
    select-aliasing fake to reuse here -- this reproduces PostgREST's own
    aliasing rules by hand so the test proves persist_locked_review's output
    shape is what the REAL matcher config expects, without modifying
    matching.py itself."""

    metadata = (row.get("result") or {}).get("metadata") or {}
    return {
        "record_id": row["id"],
        "account_number": metadata.get("account_number"),
        "owner_name": metadata.get("owner_name"),
        "tax_year": row.get("tax_year"),
    }


def test_persisted_row_is_readable_by_the_real_matcher(fake_supabase, monkeypatch):
    """persist -> reload -> MatchCandidate -> matcher sees it (spec item 19).
    Uses the real fetch_candidate_records/MatchCandidate/match_closure_to_account
    functions -- not a hand-built stand-in candidate."""

    outcome = persist_locked_review(
        district_id="district-1", file_name="a.pdf", result=PIPELINE_RESULT,
        account_number="P0001234", final_value=1.0,
    )
    stored_row = fake_supabase.parsed_rendition_results[outcome.parsed_rendition_result_id]
    postgrest_shaped_response = [_postgrest_select_shape(stored_row)]
    monkeypatch.setattr(matching.requests, "get", lambda *a, **kw: FakeHttpResponse(postgrest_shaped_response))

    candidates = matching.fetch_candidate_records("district-1")

    assert len(candidates) == 1
    assert candidates[0].account_number == "P0001234"
    assert candidates[0].owner_name == "ACME HARDWARE LLC"
    assert candidates[0].tax_year == 2026

    match_result = matching.match_closure_to_account(
        district_id="district-1", permit_legal_name="ACME HARDWARE LLC",
        permit_location_name="ACME HARDWARE", candidates=candidates,
    )
    assert match_result.confidence == "MEDIUM"  # strong name match, no property corroboration
    assert match_result.candidate.account_number == "P0001234"
