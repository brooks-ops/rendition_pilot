from __future__ import annotations

import pytest

from app.comptroller import jurisdictions, matching, new_business, service
from app.comptroller.jurisdictions import Jurisdiction
from app.comptroller.matching import MatchResult
from tests.comptroller_fakes import FakeSupabase


@pytest.fixture
def fake_supabase(monkeypatch):
    fake = FakeSupabase()
    monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "fake-service-role-key")
    # Every module that did `from app.comptroller.service import _request_json`
    # got its own binding at import time -- each needs patching separately
    # (same established pattern as tests/test_comptroller_month_end.py).
    monkeypatch.setattr(service, "_request_json", fake.request_json)
    monkeypatch.setattr(new_business, "_request_json", fake.request_json)
    monkeypatch.setattr(jurisdictions, "_request_json", fake.request_json)
    return fake


class FakeAccountsResponse:
    def __init__(self, rows):
        self.status_code = 200
        self._rows = rows
        self.text = ""

    def json(self):
        return self._rows


def set_rendition_records(monkeypatch, rows):
    """rows: list of {record_id, account_number, owner_name, tax_year} dicts,
    matching what matching.fetch_candidate_records parses from a real
    PostgREST response."""

    monkeypatch.setattr(matching.requests, "get", lambda *a, **kw: FakeAccountsResponse(rows))


def seed_jurisdiction(fake_supabase, **overrides) -> Jurisdiction:
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
    return jurisdictions._row_to_jurisdiction(row)


def add_permit(fake_supabase, tp, loc, *, county="Lubbock", location_name="SAMPLE BUSINESS", legal_name="SAMPLE BUSINESS LLC", is_baseline=False, current_status="ACTIVE", first_seen_at="2026-08-15T00:00:00+00:00"):
    key = f"{tp}::{loc}"
    row = {
        "id": f"loc-{tp}-{loc}",
        "taxpayer_id": tp,
        "location_number": loc,
        "county": county,
        "legal_name": legal_name,
        "location_name": location_name,
        "address": "100 MAIN ST",
        "city": "LUBBOCK",
        "state": "TX",
        "zip": "79401",
        "permit_start_date": "2026-08-01",
        "permit_end_date": None,
        "current_status": current_status,
        "is_baseline": is_baseline,
        "first_seen_at": first_seen_at,
        "source_dataset_id": "3kx8-uryv",
        "new_business_evaluated_at": None,
    }
    fake_supabase.permit_locations[key] = row
    return row


# -- classify_match: pure unit tests, no I/O ---------------------------------


def test_classify_high_confidence_suppressed_by_default():
    result = MatchResult(confidence="HIGH", score=1.0, reason="", candidate=None)
    classification, priority = new_business.classify_match(result)
    assert classification == "EXISTING_ACCOUNT_HIGH_CONFIDENCE"
    assert priority == "LOW"


def test_classify_medium_confidence_is_possible_existing():
    result = MatchResult(confidence="MEDIUM", score=0.9, reason="", candidate=None)
    assert new_business.classify_match(result) == ("POSSIBLE_EXISTING_ACCOUNT", "MEDIUM")


def test_classify_low_confidence_is_possible_existing():
    result = MatchResult(confidence="LOW", score=0.65, reason="", candidate=None)
    assert new_business.classify_match(result) == ("POSSIBLE_EXISTING_ACCOUNT", "MEDIUM")


def test_classify_unmatched_is_no_account_found_high_priority():
    result = MatchResult(confidence="UNMATCHED", score=0.0, reason="", candidate=None)
    assert new_business.classify_match(result) == ("NO_ACCOUNT_FOUND", "HIGH")


def test_classify_ambiguous_overrides_confidence_tier():
    result = MatchResult(confidence="HIGH", score=1.0, reason="", candidate=None, ambiguous=True)
    assert new_business.classify_match(result) == ("AMBIGUOUS", "MEDIUM")


# -- run_new_business_detection: integration tests ---------------------------


def test_new_business_with_no_cad_match_creates_high_priority_item(fake_supabase, monkeypatch):
    seed_jurisdiction(fake_supabase)
    add_permit(fake_supabase, "1", "1", location_name="JOE'S SPORTS BAR", legal_name="JOE LAMONT DOLLAR")
    set_rendition_records(monkeypatch, [])  # no RenditionPilot records at all

    result = new_business.run_new_business_detection("jur-lubbock")

    assert result.evaluated == 1
    assert result.no_account_found == 1
    assert result.items_created == 1
    item = next(iter(fake_supabase.intelligence_items.values()))
    assert item["classification"] == "NO_ACCOUNT_FOUND"
    assert item["priority"] == "HIGH"
    assert item["signal_type"] == "new_business"
    assert item["business_name"] == "JOE'S SPORTS BAR"
    assert item["recommended_action"] == "Review for possible new BPP account."


def test_exact_name_match_is_possible_existing_not_suppressed(fake_supabase, monkeypatch):
    """HIGH confidence is unreachable by the current name-only matcher (see
    matching.py's module docstring) -- even an exact name match can only
    reach MEDIUM, so it is never auto-suppressed as EXISTING_ACCOUNT_HIGH_CONFIDENCE
    today. This documents real behavior, not the spec's aspirational example."""

    seed_jurisdiction(fake_supabase)
    add_permit(fake_supabase, "1", "1", location_name="ACME HARDWARE", legal_name="ACME HARDWARE LLC")
    set_rendition_records(monkeypatch, [{"record_id": "r1", "account_number": "A100", "owner_name": "ACME HARDWARE LLC", "tax_year": 2026}])

    result = new_business.run_new_business_detection("jur-lubbock")

    assert result.possible_existing == 1
    assert result.existing_high_confidence == 0
    assert result.items_created == 1
    item = next(iter(fake_supabase.intelligence_items.values()))
    assert item["classification"] == "POSSIBLE_EXISTING_ACCOUNT"
    assert item["matched_owner_name"] == "ACME HARDWARE LLC"


def test_dba_match_with_legal_name_mismatch_still_matches_and_flags_divergence(fake_supabase, monkeypatch):
    """XYZ HOSPITALITY LLC DBA JOE'S SPORTS BAR: strong evidence via the DBA
    should matter even though the legal taxpayer name differs -- and the
    divergence itself should be flagged for a human, not silently ignored."""

    seed_jurisdiction(fake_supabase)
    add_permit(fake_supabase, "1", "1", location_name="JOE'S SPORTS BAR", legal_name="XYZ HOSPITALITY LLC")
    set_rendition_records(monkeypatch, [{"record_id": "r1", "account_number": "A100", "owner_name": "JOES SPORTS BAR", "tax_year": 2026}])

    result = new_business.run_new_business_detection("jur-lubbock")

    assert result.possible_existing == 1
    item = next(iter(fake_supabase.intelligence_items.values()))
    assert item["matched_record_id"] == "r1"
    assert item["evidence"]["name_signals_diverge"] is True


def test_same_name_on_multiple_records_is_ambiguous(fake_supabase, monkeypatch):
    """Multiple RenditionPilot records score similarly for one Comptroller
    business -- must not silently pick one."""

    seed_jurisdiction(fake_supabase)
    add_permit(fake_supabase, "1", "1", location_name="SPIRIT HALLOWEEN #100", legal_name="SPIRIT HALLOWEEN SUPERSTORES LLC")
    set_rendition_records(
        monkeypatch,
        [
            {"record_id": "r1", "account_number": "A1", "owner_name": "SPIRIT HALLOWEEN SUPERSTORES LLC", "tax_year": 2025},
            {"record_id": "r2", "account_number": "A2", "owner_name": "SPIRIT HALLOWEEN SUPERSTORES LLC", "tax_year": 2026},
        ],
    )

    result = new_business.run_new_business_detection("jur-lubbock")

    assert result.ambiguous == 1
    item = next(iter(fake_supabase.intelligence_items.values()))
    assert item["classification"] == "AMBIGUOUS"
    assert item["is_ambiguous"] is True


def test_relocation_is_never_auto_classified(fake_supabase, monkeypatch):
    """Relocation/ownership-change detection requires address data RenditionPilot
    doesn't have -- a name match alone must never be silently upgraded into a
    definitive claim; it always lands in a human-review classification."""

    seed_jurisdiction(fake_supabase)
    add_permit(fake_supabase, "1", "1", location_name="ACME HARDWARE", legal_name="ACME HARDWARE LLC")
    set_rendition_records(monkeypatch, [{"record_id": "r1", "account_number": "A1", "owner_name": "ACME HARDWARE LLC", "tax_year": 2026}])

    new_business.run_new_business_detection("jur-lubbock")

    item = next(iter(fake_supabase.intelligence_items.values()))
    assert item["classification"] in ("POSSIBLE_EXISTING_ACCOUNT", "AMBIGUOUS", "NO_ACCOUNT_FOUND")
    assert item.get("resolution") is None  # never auto-resolved to RELOCATION or anything else


def test_duplicate_intelligence_prevention_on_repeated_run(fake_supabase, monkeypatch):
    seed_jurisdiction(fake_supabase)
    add_permit(fake_supabase, "1", "1", location_name="JOE'S SPORTS BAR", legal_name="JOE LAMONT DOLLAR")
    set_rendition_records(monkeypatch, [])

    new_business.run_new_business_detection("jur-lubbock")
    result2 = new_business.run_new_business_detection("jur-lubbock", reevaluate=True)

    assert result2.items_created == 0
    assert result2.items_updated == 1  # unresolved item's evidence refreshed, not duplicated
    assert len(fake_supabase.intelligence_items) == 1


def test_previously_resolved_item_is_not_recreated(fake_supabase, monkeypatch):
    seed_jurisdiction(fake_supabase)
    add_permit(fake_supabase, "1", "1", location_name="JOE'S SPORTS BAR", legal_name="JOE LAMONT DOLLAR")
    set_rendition_records(monkeypatch, [])

    new_business.run_new_business_detection("jur-lubbock")
    existing_item = next(iter(fake_supabase.intelligence_items.values()))
    existing_item["status"] = "RESOLVED"
    existing_item["resolution"] = "NEW_ACCOUNT_NEEDED"

    result2 = new_business.run_new_business_detection("jur-lubbock", reevaluate=True)

    assert result2.items_created == 0
    assert result2.items_updated == 0
    assert result2.duplicates_suppressed == 1
    # The resolved item is untouched -- historical intelligence preserved.
    assert fake_supabase.intelligence_items[existing_item["id"]]["status"] == "RESOLVED"
    assert fake_supabase.intelligence_items[existing_item["id"]]["resolution"] == "NEW_ACCOUNT_NEEDED"


def test_repeated_source_ingestion_does_not_duplicate(fake_supabase, monkeypatch):
    seed_jurisdiction(fake_supabase)
    add_permit(fake_supabase, "1", "1", location_name="JOE'S SPORTS BAR", legal_name="JOE LAMONT DOLLAR")
    set_rendition_records(monkeypatch, [])

    for _ in range(3):
        new_business.run_new_business_detection("jur-lubbock", reevaluate=True)

    assert len(fake_supabase.intelligence_items) == 1


def test_confidence_and_reasoning_persist_on_the_item(fake_supabase, monkeypatch):
    seed_jurisdiction(fake_supabase)
    add_permit(fake_supabase, "1", "1", location_name="ACME HARDWARE", legal_name="ACME HARDWARE LLC")
    set_rendition_records(monkeypatch, [{"record_id": "r1", "account_number": "A1", "owner_name": "ACME HARDWARE LLC", "tax_year": 2026}])

    new_business.run_new_business_detection("jur-lubbock")

    item = next(iter(fake_supabase.intelligence_items.values()))
    assert item["confidence"] == "MEDIUM"
    assert "owner-name match" in item["match_reason"]
    assert item["match_signals"]["business_dba_name"] in ("MATCH", "PARTIAL MATCH")
    assert "NOT AVAILABLE" in item["match_signals"]["address"]


def test_dry_run_writes_nothing(fake_supabase, monkeypatch):
    seed_jurisdiction(fake_supabase)
    add_permit(fake_supabase, "1", "1", location_name="JOE'S SPORTS BAR", legal_name="JOE LAMONT DOLLAR")
    set_rendition_records(monkeypatch, [])

    result = new_business.run_new_business_detection("jur-lubbock", dry_run=True)

    assert result.evaluated == 1
    assert result.no_account_found == 1
    assert result.items_created == 0
    assert fake_supabase.intelligence_items == {}
    # dry run must not mark the permit evaluated either -- a real run afterward
    # should still see it as a genuine candidate.
    assert fake_supabase.permit_locations["1::1"]["new_business_evaluated_at"] is None


def test_evaluated_permits_are_skipped_on_the_next_run(fake_supabase, monkeypatch):
    seed_jurisdiction(fake_supabase)
    add_permit(fake_supabase, "1", "1", location_name="JOE'S SPORTS BAR", legal_name="JOE LAMONT DOLLAR")
    set_rendition_records(monkeypatch, [])

    first = new_business.run_new_business_detection("jur-lubbock")
    second = new_business.run_new_business_detection("jur-lubbock")  # no --reevaluate

    assert first.evaluated == 1
    assert second.evaluated == 0


def test_baseline_permits_are_never_new_business_candidates(fake_supabase, monkeypatch):
    seed_jurisdiction(fake_supabase)
    add_permit(fake_supabase, "1", "1", is_baseline=True)
    set_rendition_records(monkeypatch, [])

    result = new_business.run_new_business_detection("jur-lubbock")

    assert result.evaluated == 0


def test_inactive_permits_are_never_new_business_candidates(fake_supabase, monkeypatch):
    seed_jurisdiction(fake_supabase)
    add_permit(fake_supabase, "1", "1", current_status="INACTIVE")
    set_rendition_records(monkeypatch, [])

    result = new_business.run_new_business_detection("jur-lubbock")

    assert result.evaluated == 0


def test_jurisdiction_scoping_ignores_other_counties(fake_supabase, monkeypatch):
    seed_jurisdiction(fake_supabase)
    add_permit(fake_supabase, "1", "1", county="Lubbock", location_name="LUBBOCK BIZ", legal_name="LUBBOCK BIZ LLC")
    add_permit(fake_supabase, "2", "1", county="Dallam", location_name="DALLAM BIZ", legal_name="DALLAM BIZ LLC")
    set_rendition_records(monkeypatch, [])

    result = new_business.run_new_business_detection("jur-lubbock")

    assert result.evaluated == 1
    item = next(iter(fake_supabase.intelligence_items.values()))
    assert item["business_name"] == "LUBBOCK BIZ"


def test_capability_not_enabled_raises(fake_supabase, monkeypatch):
    seed_jurisdiction(fake_supabase, capabilities={"new_business_detection": False})
    set_rendition_records(monkeypatch, [])

    with pytest.raises(new_business.NewBusinessDetectionError):
        new_business.run_new_business_detection("jur-lubbock")
