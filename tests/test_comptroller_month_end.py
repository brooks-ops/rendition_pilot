from __future__ import annotations

from datetime import date

import pytest

from app.comptroller import month_end, service
from app.comptroller.matching import MatchResult
from tests.comptroller_fakes import FakeSupabase


@pytest.fixture
def fake_supabase(monkeypatch):
    fake = FakeSupabase()
    monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "fake-service-role-key")
    monkeypatch.setattr(month_end, "_request_json", fake.request_json)
    monkeypatch.setattr(service, "_request_json", fake.request_json)
    return fake


@pytest.fixture(autouse=True)
def stub_matching(monkeypatch):
    # MEDIUM, not HIGH: HIGH is unreachable by the real matcher (no address/ZIP
    # data exists to corroborate a name match -- see matching.py's docstring),
    # so a stub returning HIGH would misrepresent what these tests exercise.
    result = MatchResult(confidence="MEDIUM", score=0.9, reason="stubbed for month-end tests", candidate=None)
    monkeypatch.setattr(month_end, "match_closure_to_account", lambda **kwargs: result)
    # process_month_end fetches+caches each district's rendition-record list
    # directly (to avoid re-fetching per event); stub it too so tests never
    # hit the network even though match_closure_to_account itself is also
    # stubbed.
    monkeypatch.setattr(month_end, "fetch_candidate_records", lambda district_id: [])


@pytest.fixture(autouse=True)
def stub_emailer(monkeypatch):
    """No test should attempt a real SMTP connection. Tests exercising the
    email step specifically override this per-test."""

    sent = []
    monkeypatch.setattr(
        month_end,
        "send_month_end_export_email",
        lambda month_label, xlsx_bytes, review_count=0: sent.append((month_label, review_count)),
    )
    return sent


def add_location(fake, tp, loc, **fields):
    key = f"{tp}::{loc}"
    row = {
        "id": fake._new_id("loc"),
        "taxpayer_id": tp,
        "location_number": loc,
        "district_id": "district-1",
        "county": "Lubbock",
        "legal_name": "SAMPLE TAXPAYER INC",
        "location_name": "SAMPLE LOCATION",
        "address": "100 MAIN ST",
        "city": "LUBBOCK",
        "state": "TX",
        "zip": "79401",
        "permit_start_date": "2010-01-01",
        "permit_end_date": None,
        "current_status": "ACTIVE",
    }
    row.update(fields)
    fake.permit_locations[key] = row
    return row


def add_event(fake, location, *, detected_at, change_type="STATUS_CHANGE", processed=False):
    row = {
        "id": fake._new_id("evt"),
        "taxpayer_id": location["taxpayer_id"],
        "location_number": location["location_number"],
        "permit_location_id": location["id"],
        "change_type": change_type,
        "previous_status": "ACTIVE",
        "new_status": "INACTIVE",
        "previous_permit_end_date": None,
        "new_permit_end_date": "2026-08-12",
        "detected_at": detected_at,
        "source_data_date": None,
        "sync_run_id": None,
        "month_end_processed_at": "2026-01-01T00:00:00+00:00" if processed else None,
        "review_item_id": None,
    }
    fake.status_events[row["id"]] = row
    return row


# -- month_bounds: 28/29/30/31-day months + year rollover --------------------


def test_month_bounds_31_day_month():
    assert month_end.month_bounds(date(2026, 1, 15)) == (date(2026, 1, 1), date(2026, 2, 1))


def test_month_bounds_30_day_month():
    assert month_end.month_bounds(date(2026, 4, 3)) == (date(2026, 4, 1), date(2026, 5, 1))


def test_month_bounds_28_day_february_non_leap_year():
    assert month_end.month_bounds(date(2026, 2, 10)) == (date(2026, 2, 1), date(2026, 3, 1))


def test_month_bounds_29_day_february_leap_year():
    assert month_end.month_bounds(date(2024, 2, 10)) == (date(2024, 2, 1), date(2024, 3, 1))


def test_month_bounds_december_rolls_into_january():
    assert month_end.month_bounds(date(2026, 12, 5)) == (date(2026, 12, 1), date(2027, 1, 1))


# -- resolve_target_month -----------------------------------------------------


def test_resolve_target_month_defaults_to_previous_month():
    assert month_end.resolve_target_month(None, today=date(2026, 8, 18)) == date(2026, 7, 1)


def test_resolve_target_month_handles_year_rollover():
    assert month_end.resolve_target_month(None, today=date(2027, 1, 5)) == date(2026, 12, 1)


def test_resolve_target_month_explicit_override():
    assert month_end.resolve_target_month("2026-08") == date(2026, 8, 1)


def test_resolve_target_month_rejects_invalid_format():
    with pytest.raises(ValueError):
        month_end.resolve_target_month("not-a-month")


# -- process_month_end: correct month scoping + idempotency ------------------


def test_process_month_end_only_processes_target_months_events(fake_supabase):
    loc = add_location(fake_supabase, "1", "1")
    aug_event = add_event(fake_supabase, loc, detected_at="2026-08-12T00:00:00+00:00")
    sept_event = add_event(fake_supabase, loc, detected_at="2026-09-05T00:00:00+00:00")

    result = month_end.process_month_end(date(2026, 8, 1))

    assert result.candidates_processed == 1
    assert fake_supabase.status_events[aug_event["id"]]["month_end_processed_at"] is not None
    assert fake_supabase.status_events[sept_event["id"]]["month_end_processed_at"] is None


def test_baseline_events_are_never_month_end_processed(fake_supabase):
    loc = add_location(fake_supabase, "1", "1")
    add_event(fake_supabase, loc, detected_at="2026-08-01T00:00:00+00:00", change_type="BASELINE")

    result = month_end.process_month_end(date(2026, 8, 1))

    assert result.candidates_processed == 0
    assert fake_supabase.closure_reviews == {}


def test_reopened_events_are_never_turned_into_reviews(fake_supabase):
    loc = add_location(fake_supabase, "1", "1")
    add_event(fake_supabase, loc, detected_at="2026-08-01T00:00:00+00:00", change_type="REOPENED")

    result = month_end.process_month_end(date(2026, 8, 1))

    assert result.candidates_processed == 0
    assert fake_supabase.closure_reviews == {}


def test_already_processed_events_are_not_reprocessed(fake_supabase):
    loc = add_location(fake_supabase, "1", "1")
    add_event(fake_supabase, loc, detected_at="2026-08-05T00:00:00+00:00", processed=True)
    fresh_event = add_event(fake_supabase, loc, detected_at="2026-08-12T00:00:00+00:00")

    result = month_end.process_month_end(date(2026, 8, 1))

    assert result.candidates_processed == 1
    reviews = list(fake_supabase.closure_reviews.values())
    assert len(reviews) == 1
    assert reviews[0]["status_event_id"] == fresh_event["id"]


def test_rerunning_month_end_does_not_duplicate_reviews(fake_supabase):
    loc = add_location(fake_supabase, "1", "1")
    add_event(fake_supabase, loc, detected_at="2026-08-12T00:00:00+00:00")

    first = month_end.process_month_end(date(2026, 8, 1))
    second = month_end.process_month_end(date(2026, 8, 1))

    assert first.candidates_processed == 1
    assert second.candidates_processed == 0
    assert len(fake_supabase.closure_reviews) == 1


def test_dry_run_does_not_write_reviews_or_mark_events_processed(fake_supabase):
    loc = add_location(fake_supabase, "1", "1")
    event = add_event(fake_supabase, loc, detected_at="2026-08-12T00:00:00+00:00")

    result = month_end.process_month_end(date(2026, 8, 1), dry_run=True)

    assert result.candidates_processed == 1
    assert fake_supabase.closure_reviews == {}
    assert fake_supabase.status_events[event["id"]]["month_end_processed_at"] is None
    assert result.email_sent is False


# -- monthly export email -----------------------------------------------------


def test_real_run_sends_the_export_email(fake_supabase, stub_emailer):
    loc = add_location(fake_supabase, "1", "1")
    add_event(fake_supabase, loc, detected_at="2026-08-12T00:00:00+00:00")

    result = month_end.process_month_end(date(2026, 8, 1))

    assert result.email_sent is True
    assert result.email_error is None
    assert stub_emailer == [("2026-08", 1)]


def test_dry_run_never_sends_an_email(fake_supabase, stub_emailer):
    loc = add_location(fake_supabase, "1", "1")
    add_event(fake_supabase, loc, detected_at="2026-08-12T00:00:00+00:00")

    result = month_end.process_month_end(date(2026, 8, 1), dry_run=True)

    assert result.email_sent is False
    assert stub_emailer == []


def test_email_failure_does_not_fail_the_month_end_run(fake_supabase, monkeypatch):
    from app.comptroller.emailer import EmailDeliveryError

    def fail_send(month_label, xlsx_bytes, review_count=0):
        raise EmailDeliveryError("SMTP server unavailable")

    monkeypatch.setattr(month_end, "send_month_end_export_email", fail_send)

    loc = add_location(fake_supabase, "1", "1")
    event = add_event(fake_supabase, loc, detected_at="2026-08-12T00:00:00+00:00")

    result = month_end.process_month_end(date(2026, 8, 1))

    # The review itself was still created and the event still marked
    # processed -- only the notification failed.
    assert result.candidates_processed == 1
    assert len(fake_supabase.closure_reviews) == 1
    assert fake_supabase.status_events[event["id"]]["month_end_processed_at"] is not None
    assert result.email_sent is False
    assert "SMTP server unavailable" in result.email_error


def test_email_sent_even_with_zero_candidates(fake_supabase, stub_emailer):
    """A quiet month should still confirm the pipeline ran, per the user's
    request for the export "every month" -- not only when something happened."""

    result = month_end.process_month_end(date(2026, 8, 1))

    assert result.candidates_processed == 0
    assert result.email_sent is True
    assert stub_emailer == [("2026-08", 0)]


def test_review_item_captures_comptroller_and_match_fields(fake_supabase):
    loc = add_location(fake_supabase, "1", "1", location_name="ACME HARDWARE")
    add_event(fake_supabase, loc, detected_at="2026-08-12T00:00:00+00:00")

    month_end.process_month_end(date(2026, 8, 1))

    review = next(iter(fake_supabase.closure_reviews.values()))
    assert review["comptroller_business_name"] == "ACME HARDWARE"
    assert review["match_confidence"] == "MEDIUM"
    assert review["workflow_status"] == "PENDING_REVIEW"


def test_ambiguous_match_result_survives_into_the_review_record(fake_supabase, monkeypatch):
    ambiguous_result = MatchResult(
        confidence="MEDIUM",
        score=1.0,
        reason="strong owner-name match; ambiguous -- 1 other record(s) scored similarly",
        candidate=None,
        ambiguous=True,
    )
    monkeypatch.setattr(month_end, "match_closure_to_account", lambda **kwargs: ambiguous_result)

    loc = add_location(fake_supabase, "1", "1")
    add_event(fake_supabase, loc, detected_at="2026-08-12T00:00:00+00:00")

    month_end.process_month_end(date(2026, 8, 1))

    review = next(iter(fake_supabase.closure_reviews.values()))
    assert review["match_ambiguous"] is True
    assert "ambiguous" in review["match_reason"]


def test_account_candidates_are_fetched_once_per_district_per_run(fake_supabase, monkeypatch):
    fetch_calls = []
    monkeypatch.setattr(
        month_end,
        "fetch_candidate_records",
        lambda district_id: (fetch_calls.append(district_id), [])[1],
    )

    loc1 = add_location(fake_supabase, "1", "1")
    loc2 = add_location(fake_supabase, "2", "1")
    add_event(fake_supabase, loc1, detected_at="2026-08-05T00:00:00+00:00")
    add_event(fake_supabase, loc2, detected_at="2026-08-12T00:00:00+00:00")

    result = month_end.process_month_end(date(2026, 8, 1))

    assert result.candidates_processed == 2
    assert fetch_calls == ["district-1"]  # both locations share district-1; fetched only once


def test_retrying_a_review_conflict_still_backfills_review_item_id(fake_supabase):
    loc = add_location(fake_supabase, "1", "1")
    event = add_event(fake_supabase, loc, detected_at="2026-08-12T00:00:00+00:00")

    # Simulate a review that was already created by an earlier, interrupted
    # run (before the event got marked processed).
    fake_supabase.closure_reviews["review-existing"] = {
        "id": "review-existing",
        "status_event_id": event["id"],
    }

    month_end.process_month_end(date(2026, 8, 1))

    assert fake_supabase.status_events[event["id"]]["review_item_id"] == "review-existing"
    assert fake_supabase.status_events[event["id"]]["month_end_processed_at"] is not None
    assert len(fake_supabase.closure_reviews) == 1
