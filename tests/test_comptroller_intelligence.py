from __future__ import annotations

import pytest

from app.comptroller import admin, intelligence, service
from tests.comptroller_fakes import FakeSupabase


@pytest.fixture
def fake_supabase(monkeypatch):
    fake = FakeSupabase()
    monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "fake-service-role-key")
    monkeypatch.setattr(service, "_request_json", fake.request_json)
    monkeypatch.setattr(intelligence, "_request_json", fake.request_json)
    monkeypatch.setattr(admin, "_request_json", fake.request_json)
    return fake


def add_intelligence_item(fake_supabase, **overrides):
    row = {
        "id": f"intel-{len(fake_supabase.intelligence_items) + 1}",
        "district_id": "district-1",
        "jurisdiction_id": "jur-lubbock",
        "signal_type": "new_business",
        "source": "tx_comptroller_open_data",
        "source_record_id": "1:1",
        "status": "NEW",
        "classification": "NO_ACCOUNT_FOUND",
        "priority": "HIGH",
        "confidence": "UNMATCHED",
        "is_ambiguous": False,
        "business_name": "JOE'S SPORTS BAR",
        "legal_name": "JOE LAMONT DOLLAR",
        "source_city": "LUBBOCK",
        "first_detected_at": "2026-08-15T00:00:00+00:00",
        "match_reason": "No RenditionPilot record met the minimum name-similarity threshold.",
        "match_signals": {"business_dba_name": "NO MATCH"},
        "recommended_action": "Review for possible new BPP account.",
        "created_at": "2026-08-15T00:00:00+00:00",
    }
    row.update(overrides)
    fake_supabase.intelligence_items[row["id"]] = row
    return row


def add_closure_review(fake_supabase, **overrides):
    row = {
        "id": f"review-{len(fake_supabase.closure_reviews) + 1}",
        "district_id": "district-1",
        "comptroller_business_name": "OLD RESTAURANT",
        "comptroller_city": "LUBBOCK",
        "workflow_status": "PENDING_REVIEW",
        "match_confidence": "UNMATCHED",
        "match_ambiguous": False,
        "first_detected_at": "2026-08-10T00:00:00+00:00",
        "created_at": "2026-08-10T00:00:00+00:00",
    }
    row.update(overrides)
    fake_supabase.closure_reviews[row["id"]] = row
    return row


# -- unified listing -----------------------------------------------------


def test_queue_merges_both_sources(fake_supabase):
    add_intelligence_item(fake_supabase)
    add_closure_review(fake_supabase)

    items = intelligence.list_intelligence_queue("district-1")

    signal_types = {item.signal_type for item in items}
    assert signal_types == {"new_business", "sales_tax_inactive"}


def test_queue_scoped_to_district(fake_supabase):
    add_intelligence_item(fake_supabase, district_id="district-1")
    add_intelligence_item(fake_supabase, district_id="district-other")

    items = intelligence.list_intelligence_queue("district-1")

    assert len(items) == 1
    assert items[0].raw["district_id"] == "district-1"


def test_filter_by_signal_type(fake_supabase):
    add_intelligence_item(fake_supabase)
    add_closure_review(fake_supabase)

    items = intelligence.list_intelligence_queue("district-1", signal_type="new_business")

    assert len(items) == 1
    assert items[0].signal_type == "new_business"


def test_filter_by_status(fake_supabase):
    add_intelligence_item(fake_supabase, status="NEW")
    add_intelligence_item(fake_supabase, status="RESOLVED")

    items = intelligence.list_intelligence_queue("district-1", status="NEW")

    assert len(items) == 1
    assert items[0].status == "NEW"


def test_closure_review_workflow_status_maps_to_shared_lifecycle(fake_supabase):
    add_closure_review(fake_supabase, workflow_status="CONFIRMED_CLOSURE")
    add_closure_review(fake_supabase, workflow_status="NOT_CLOSED")
    add_closure_review(fake_supabase, workflow_status="PENDING_REVIEW")

    items = intelligence.list_intelligence_queue("district-1")
    statuses = sorted(item.status for item in items)

    assert statuses == ["DISMISSED", "NEW", "RESOLVED"]


def test_filter_by_city_case_insensitive(fake_supabase):
    add_intelligence_item(fake_supabase, source_city="Lubbock")

    items = intelligence.list_intelligence_queue("district-1", city="LUBBOCK")

    assert len(items) == 1


# -- summary counts -----------------------------------------------------


def test_summary_counts(fake_supabase):
    add_intelligence_item(fake_supabase, status="NEW", priority="HIGH")
    add_intelligence_item(fake_supabase, status="IN_REVIEW", priority="MEDIUM")
    add_closure_review(fake_supabase, workflow_status="CONFIRMED_CLOSURE")
    add_closure_review(fake_supabase, workflow_status="PENDING_REVIEW")

    summary = intelligence.get_queue_summary("district-1")

    assert summary["new"] == 2  # 1 bpp_intelligence_items NEW + 1 closure PENDING_REVIEW->NEW
    assert summary["high_priority"] == 1
    assert summary["needs_review"] == 1
    assert summary["resolved"] == 1
    assert summary["total"] == 4


# -- actions: investigate/resolve/dismiss route to the right table -----------


def test_investigate_intelligence_item(fake_supabase):
    row = add_intelligence_item(fake_supabase)

    item = intelligence.investigate_item(intelligence.SOURCE_TABLE_INTELLIGENCE, row["id"], assigned_to="user-1")

    assert item.status == "IN_REVIEW"
    assert fake_supabase.intelligence_items[row["id"]]["assigned_to"] == "user-1"


def test_investigate_closure_review(fake_supabase):
    row = add_closure_review(fake_supabase)

    item = intelligence.investigate_item(intelligence.SOURCE_TABLE_CLOSURE_REVIEW, row["id"])

    assert fake_supabase.closure_reviews[row["id"]]["workflow_status"] == "OTHER_NEEDS_RESEARCH"
    assert item.status == "IN_REVIEW"


def test_resolve_intelligence_item_sets_resolution(fake_supabase):
    row = add_intelligence_item(fake_supabase)

    item = intelligence.resolve_item(
        intelligence.SOURCE_TABLE_INTELLIGENCE, row["id"],
        resolution="NEW_ACCOUNT_NEEDED", resolution_notes="Confirmed new business", reviewed_by="user-1",
    )

    assert item.status == "RESOLVED"
    assert fake_supabase.intelligence_items[row["id"]]["resolution"] == "NEW_ACCOUNT_NEEDED"
    assert fake_supabase.intelligence_items[row["id"]]["resolution_notes"] == "Confirmed new business"
    assert fake_supabase.intelligence_items[row["id"]]["reviewed_by"] == "user-1"
    assert fake_supabase.intelligence_items[row["id"]]["reviewed_at"] is not None


def test_resolve_closure_review_maps_resolution_to_workflow_status(fake_supabase):
    row = add_closure_review(fake_supabase)

    intelligence.resolve_item(
        intelligence.SOURCE_TABLE_CLOSURE_REVIEW, row["id"],
        resolution="BUSINESS_CLOSED", resolution_notes="Verified closed", reviewed_by="user-1",
    )

    assert fake_supabase.closure_reviews[row["id"]]["workflow_status"] == "CONFIRMED_CLOSURE"
    assert fake_supabase.closure_reviews[row["id"]]["reviewer_notes"] == "Verified closed"


def test_dismiss_intelligence_item(fake_supabase):
    row = add_intelligence_item(fake_supabase)

    item = intelligence.dismiss_item(intelligence.SOURCE_TABLE_INTELLIGENCE, row["id"], resolution_notes="Not real", reviewed_by="user-1")

    assert item.status == "DISMISSED"
    assert fake_supabase.intelligence_items[row["id"]]["resolution"] == "FALSE_MATCH"


def test_dismiss_closure_review_maps_to_not_closed(fake_supabase):
    row = add_closure_review(fake_supabase)

    intelligence.dismiss_item(intelligence.SOURCE_TABLE_CLOSURE_REVIEW, row["id"], resolution_notes=None, reviewed_by="user-1")

    assert fake_supabase.closure_reviews[row["id"]]["workflow_status"] == "NOT_CLOSED"


def test_resolving_never_touches_appraisal_fields(fake_supabase):
    """The whole point of this queue: only its own status/resolution/notes/
    reviewer fields are ever written -- never property value, ownership,
    account status, BPP records, or exemptions."""

    row = add_intelligence_item(fake_supabase)
    allowed_keys = {"status", "resolution", "resolution_notes", "reviewed_by", "reviewed_at", "assigned_to"}

    intelligence.resolve_item(
        intelligence.SOURCE_TABLE_INTELLIGENCE, row["id"], resolution="OTHER", resolution_notes="note", reviewed_by="u1",
    )

    patch_calls = [c for c in fake_supabase.calls if c["method"] == "PATCH" and c["url"].endswith("bpp_intelligence_items")]
    assert patch_calls
    for call in patch_calls:
        assert set(call["json_payload"].keys()) <= allowed_keys


def test_unknown_source_table_raises(fake_supabase):
    with pytest.raises(intelligence.IntelligenceQueueError):
        intelligence.get_intelligence_item("not_a_real_table", "x")
