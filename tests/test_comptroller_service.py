from __future__ import annotations

from datetime import date

import pytest

from app.comptroller import client as comptroller_client
from app.comptroller import service
from tests.comptroller_fakes import FakeSupabase


@pytest.fixture
def fake_supabase(monkeypatch):
    fake = FakeSupabase()
    monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "fake-service-role-key")
    monkeypatch.setattr(service, "_request_json", fake.request_json)
    return fake


def make_record(tp="17512000001", loc="1", status="ACTIVE", end_date=None, **overrides):
    defaults = dict(
        taxpayer_id=tp,
        location_number=loc,
        legal_name="SAMPLE TAXPAYER INC",
        location_name="SAMPLE LOCATION",
        address="100 MAIN ST",
        city="LUBBOCK",
        state="TX",
        zip="79401",
        county_code="152",
        permit_start_date=date(2010, 1, 1),
        permit_end_date=end_date,
        current_status=status,
        raw={},
    )
    defaults.update(overrides)
    return comptroller_client.PermitRecord(**defaults)


def make_fetch_result(records, county_code="152"):
    return comptroller_client.ComptrollerFetchResult(
        records=records,
        skipped_row_count=0,
        source_data_date=None,
        dataset_id="3kx8-uryv",
        county_code=county_code,
    )


def set_fetch(monkeypatch, records):
    monkeypatch.setattr(
        service.comptroller_client,
        "fetch_county_permits",
        lambda *args, **kwargs: make_fetch_result(records),
    )


def non_baseline_events(fake_supabase):
    return [e for e in fake_supabase.status_events.values() if e["change_type"] != "BASELINE"]


# 1. Baseline import creates no false closure events -------------------------


def test_baseline_import_creates_no_closure_events(fake_supabase, monkeypatch):
    records = [
        make_record(tp="1", loc="1", status="ACTIVE"),
        make_record(tp="2", loc="1", status="INACTIVE", end_date=date(2020, 1, 1)),
    ]
    set_fetch(monkeypatch, records)

    result = service.sync_county("Lubbock")

    assert result.run_type == service.RUN_TYPE_BASELINE
    assert result.permits_checked == 2
    assert result.permits_new == 2
    assert result.permits_newly_inactive == 0
    events = list(fake_supabase.status_events.values())
    assert len(events) == 2
    assert all(e["change_type"] == "BASELINE" for e in events)
    assert non_baseline_events(fake_supabase) == []


# 2. ACTIVE -> ACTIVE creates no change --------------------------------------


def test_active_to_active_creates_no_change(fake_supabase, monkeypatch):
    set_fetch(monkeypatch, [make_record(status="ACTIVE")])
    service.sync_county("Lubbock")  # baseline

    set_fetch(monkeypatch, [make_record(status="ACTIVE")])
    result = service.sync_county("Lubbock")

    assert result.run_type == service.RUN_TYPE_DAILY
    assert result.permits_newly_inactive == 0
    assert non_baseline_events(fake_supabase) == []


# 3 & 4. detect_change unit coverage for each independent condition ----------


def test_detect_change_status_flip_only():
    prior = {"current_status": "ACTIVE", "permit_end_date": None}
    record = make_record(status="INACTIVE", end_date=None)
    changed, change_type = service.detect_change(prior, record)
    assert changed and change_type == "STATUS_CHANGE"


def test_detect_change_permit_end_date_added_only():
    prior = {"current_status": "ACTIVE", "permit_end_date": None}
    record = make_record(status="ACTIVE", end_date=date(2026, 3, 2))
    changed, change_type = service.detect_change(prior, record)
    assert changed and change_type == "PERMIT_END_DATE_ADDED"


# 5. Combined status + end-date change is ONE logical event ------------------


def test_detect_change_combined_status_and_end_date_is_single_event():
    prior = {"current_status": "ACTIVE", "permit_end_date": None}
    record = make_record(status="INACTIVE", end_date=date(2026, 3, 2))
    changed, change_type = service.detect_change(prior, record)
    assert changed and change_type == "STATUS_AND_END_DATE_CHANGE"


def test_sync_county_realistic_closure_creates_exactly_one_event(fake_supabase, monkeypatch):
    set_fetch(monkeypatch, [make_record(status="ACTIVE")])
    service.sync_county("Lubbock")  # baseline

    set_fetch(monkeypatch, [make_record(status="INACTIVE", end_date=date(2026, 3, 2))])
    result = service.sync_county("Lubbock")

    assert result.permits_newly_inactive == 1
    events = non_baseline_events(fake_supabase)
    assert len(events) == 1
    assert events[0]["change_type"] == "STATUS_AND_END_DATE_CHANGE"


# 6. Repeated daily runs do not duplicate a closure --------------------------


def test_repeated_daily_runs_do_not_duplicate_closure(fake_supabase, monkeypatch):
    set_fetch(monkeypatch, [make_record(status="ACTIVE")])
    service.sync_county("Lubbock")  # baseline

    closed_record = make_record(status="INACTIVE", end_date=date(2026, 3, 2))
    set_fetch(monkeypatch, [closed_record])
    service.sync_county("Lubbock")  # day 1: detects closure
    service.sync_county("Lubbock")  # day 2: same state, must not duplicate
    service.sync_county("Lubbock")  # day 3: still stable

    events = non_baseline_events(fake_supabase)
    assert len(events) == 1


def test_dedup_guard_does_not_treat_a_baseline_event_as_a_duplicate(fake_supabase, monkeypatch):
    """Regression test for a bug found via live production validation
    (2026-08-21): a permit that was already INACTIVE at baseline time has a
    BASELINE event whose new_status/new_permit_end_date equal its real
    current state by definition. has_unprocessed_duplicate_event must ignore
    BASELINE rows, or any later re-detection landing back on that same state
    (e.g. a stale `current_status` on the location row) gets silently
    swallowed as a "duplicate" of the baseline snapshot instead of recorded."""

    closed_record = make_record(status="INACTIVE", end_date=date(2026, 1, 31))
    set_fetch(monkeypatch, [closed_record])
    service.sync_county("Lubbock")  # baseline: BASELINE event, new_status=INACTIVE, end=2026-01-31

    # Simulate the location row's stored state going stale/reverted (exactly
    # what happened live: an interrupted upsert left it looking ACTIVE again)
    # without touching the event history, then let a real sync re-detect it.
    fake_supabase.permit_locations["17512000001::1"]["current_status"] = "ACTIVE"
    fake_supabase.permit_locations["17512000001::1"]["permit_end_date"] = None

    set_fetch(monkeypatch, [closed_record])
    result = service.sync_county("Lubbock")

    assert result.permits_newly_inactive == 1
    events = non_baseline_events(fake_supabase)
    assert len(events) == 1
    assert events[0]["change_type"] == "STATUS_AND_END_DATE_CHANGE"
    assert fake_supabase.permit_locations["17512000001::1"]["current_status"] == "INACTIVE"


def test_last_changed_at_is_set_only_when_a_change_is_detected(fake_supabase, monkeypatch):
    set_fetch(monkeypatch, [make_record(status="ACTIVE")])
    service.sync_county("Lubbock")  # baseline
    assert fake_supabase.permit_locations["17512000001::1"].get("last_changed_at") is None

    # Unchanged sync: last_changed_at must stay unset.
    set_fetch(monkeypatch, [make_record(status="ACTIVE")])
    service.sync_county("Lubbock")
    assert fake_supabase.permit_locations["17512000001::1"].get("last_changed_at") is None

    # Real change: last_changed_at must be populated.
    set_fetch(monkeypatch, [make_record(status="INACTIVE", end_date=date(2026, 3, 2))])
    service.sync_county("Lubbock")
    assert fake_supabase.permit_locations["17512000001::1"].get("last_changed_at") is not None


def test_bulk_upsert_payloads_have_identical_key_sets_in_a_mixed_batch(fake_supabase, monkeypatch):
    """Regression test for a bug found via live production validation
    (2026-08-21): PostgREST rejects a bulk insert/upsert whose objects don't
    all share the exact same keys ("All object keys must match"). A single
    sync's location_payloads always contains a mix of new, changed, and
    unchanged records -- every payload dict must have identical keys
    regardless of which of those three buckets a given record falls into."""

    set_fetch(monkeypatch, [make_record(tp="1", loc="1", status="ACTIVE"), make_record(tp="2", loc="1", status="ACTIVE")])
    service.sync_county("Lubbock")  # baseline

    # One record unchanged, one closes, one is brand new -- three different
    # payload shapes if any field were still conditionally included.
    set_fetch(
        monkeypatch,
        [
            make_record(tp="1", loc="1", status="ACTIVE"),
            make_record(tp="2", loc="1", status="INACTIVE", end_date=date(2026, 3, 2)),
            make_record(tp="3", loc="1", status="ACTIVE"),
        ],
    )
    service.sync_county("Lubbock")

    upsert_calls = [c for c in fake_supabase.calls if c["url"].endswith("comptroller_permit_locations") and c["method"] == "POST"]
    assert upsert_calls, "expected at least one bulk upsert call"
    for call in upsert_calls:
        payloads = call["json_payload"]
        key_sets = {frozenset(row.keys()) for row in payloads}
        assert len(key_sets) == 1, f"inconsistent keys across a single bulk upsert batch: {key_sets}"


# 7. A failed/empty fetch does not mark existing permits inactive -----------


def test_empty_fetch_after_healthy_baseline_aborts_without_mutating_state(fake_supabase, monkeypatch):
    records = [make_record(tp=str(i), loc="1", status="ACTIVE") for i in range(10)]
    set_fetch(monkeypatch, records)
    service.sync_county("Lubbock")  # baseline, permits_checked=10

    set_fetch(monkeypatch, [])  # simulate an outage / empty response

    with pytest.raises(service.ComptrollerServiceError):
        service.sync_county("Lubbock")

    assert len(fake_supabase.permit_locations) == 10
    assert all(row["current_status"] == "ACTIVE" for row in fake_supabase.permit_locations.values())
    failed_runs = [r for r in fake_supabase.sync_runs.values() if r["status"] == "FAILED"]
    assert len(failed_runs) == 1
    assert non_baseline_events(fake_supabase) == []


# 8. A brand-new permit on a later daily run is tracked but not a "closure" --


def test_new_permit_on_daily_run_is_not_treated_as_a_closure(fake_supabase, monkeypatch):
    set_fetch(monkeypatch, [make_record(tp="1")])
    service.sync_county("Lubbock")  # baseline

    set_fetch(monkeypatch, [make_record(tp="1"), make_record(tp="2")])
    result = service.sync_county("Lubbock")

    assert result.permits_new == 1
    assert result.permits_newly_inactive == 0
    assert non_baseline_events(fake_supabase) == []
    assert fake_supabase.permit_locations["2::1"]["first_seen_at"]


def test_reopened_permit_is_recorded_but_not_counted_as_newly_inactive(fake_supabase, monkeypatch):
    set_fetch(monkeypatch, [make_record(status="INACTIVE", end_date=date(2020, 1, 1))])
    service.sync_county("Lubbock")  # baseline (already inactive on day 1)

    set_fetch(monkeypatch, [make_record(status="ACTIVE", end_date=None)])
    result = service.sync_county("Lubbock")

    assert result.permits_newly_inactive == 0
    events = non_baseline_events(fake_supabase)
    assert len(events) == 1
    assert events[0]["change_type"] == "REOPENED"


def test_crash_between_event_write_and_location_upsert_does_not_duplicate_on_retry(fake_supabase, monkeypatch):
    """Simulates a prior sync that wrote the status event but crashed before
    upserting comptroller_permit_locations (see sync_county's write order and
    has_unprocessed_duplicate_event). A retry must not create a second event
    for the same change, even though the location row still shows the old
    (ACTIVE) state and would otherwise look like a fresh detection."""

    set_fetch(monkeypatch, [make_record(status="ACTIVE")])
    service.sync_county("Lubbock")  # baseline
    location_id = fake_supabase.permit_locations["17512000001::1"]["id"]

    # Pretend an earlier, interrupted run already wrote the closure event...
    fake_supabase.status_events["evt-partial"] = {
        "id": "evt-partial",
        "permit_location_id": location_id,
        "taxpayer_id": "17512000001",
        "location_number": "1",
        "change_type": "STATUS_AND_END_DATE_CHANGE",
        "previous_status": "ACTIVE",
        "new_status": "INACTIVE",
        "previous_permit_end_date": None,
        "new_permit_end_date": "2026-03-02",
        "month_end_processed_at": None,
    }
    # ...but never got to upsert the location row, which is still ACTIVE.
    assert fake_supabase.permit_locations["17512000001::1"]["current_status"] == "ACTIVE"

    set_fetch(monkeypatch, [make_record(status="INACTIVE", end_date=date(2026, 3, 2))])
    service.sync_county("Lubbock")  # retry

    events = non_baseline_events(fake_supabase)
    assert len(events) == 1  # no duplicate written
    assert fake_supabase.permit_locations["17512000001::1"]["current_status"] == "INACTIVE"


def test_unknown_county_raises_before_any_fetch(fake_supabase, monkeypatch):
    called = False

    def fail_fetch(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("should not fetch for an unknown county")

    monkeypatch.setattr(service.comptroller_client, "fetch_county_permits", fail_fetch)

    with pytest.raises(service.ComptrollerServiceError):
        service.sync_county("Not A Real County")

    assert called is False
