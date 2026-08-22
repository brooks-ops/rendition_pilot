"""Month-end batch processing: turn a month's unprocessed permit status
change events into review-queue items.

Runs independently of the daily sync. Daily syncs only ever write to
comptroller_permit_locations / comptroller_permit_status_events; nothing
user-facing is created until this module runs. Month boundaries are computed
with plain date arithmetic (first-of-month / first-of-next-month), which
handles 28/29/30/31-day months and year rollover (Dec -> Jan) without any
hardcoded day count. Idempotent: an event already marked
month_end_processed_at is never re-selected, and the unique index on
comptroller_closure_reviews.status_event_id means even a crash between
"insert review" and "mark event processed" can't produce a duplicate review
on retry.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.comptroller.emailer import EmailConfigError, EmailDeliveryError, send_month_end_export_email
from app.comptroller.export import build_review_queue_workbook
from app.comptroller.matching import (
    MatchCandidate,
    MatchingConfigError,
    MatchResult,
    fetch_candidate_records,
    match_closure_to_account,
)
from app.comptroller.service import (
    RUN_TYPE_MONTH_END,
    _paginated_get,
    _request_json,
    create_sync_run,
    finish_sync_run,
    get_supabase_config,
    list_all_reviews_for_month,
    postgrest_headers,
)

# BASELINE rows are the initial import, not a detected change. REOPENED means
# the permit is ACTIVE again by the time we're processing the month -- surfacing
# it as a "possible closure" would be actively misleading, so it's excluded
# from review generation the same way BASELINE is.
NON_REVIEWABLE_CHANGE_TYPES = ("BASELINE", "REOPENED")


@dataclass(frozen=True)
class MonthEndResult:
    review_month: date
    dry_run: bool
    candidates_processed: int
    matched_high: int
    matched_medium: int
    matched_low: int
    unmatched: int
    ambiguous: int = 0
    review_ids: list[str] = field(default_factory=list)
    email_sent: bool = False
    email_error: str | None = None


def month_bounds(target_month: date) -> tuple[date, date]:
    """Return (first day of target_month, first day of the following month)."""

    start = date(target_month.year, target_month.month, 1)
    days_in_month = calendar.monthrange(start.year, start.month)[1]
    end_of_month = date(start.year, start.month, days_in_month)
    end_exclusive = end_of_month + timedelta(days=1)
    return start, end_exclusive


def resolve_target_month(explicit_month: str | None, *, today: date | None = None) -> date:
    """Resolve which calendar month to process.

    Defaults to the previous calendar month relative to `today`, so the
    processor is meant to be run on/after the 1st of a month to process the
    month that just ended -- this sidesteps any "day == 31" style scheduling
    logic entirely. `explicit_month` (format "YYYY-MM") overrides this for
    manual/backfill runs.
    """

    if explicit_month:
        try:
            year_str, month_str = explicit_month.split("-", 1)
            return date(int(year_str), int(month_str), 1)
        except (ValueError, IndexError) as exc:
            raise ValueError(f"Invalid --month value '{explicit_month}', expected YYYY-MM.") from exc

    reference = today or datetime.now(timezone.utc).date()
    first_of_this_month = date(reference.year, reference.month, 1)
    last_day_of_previous_month = first_of_this_month - timedelta(days=1)
    return date(last_day_of_previous_month.year, last_day_of_previous_month.month, 1)


def get_unprocessed_events_for_month(target_month: date) -> list[dict[str, Any]]:
    start, end_exclusive = month_bounds(target_month)
    supabase_url, service_role_key = get_supabase_config()
    headers = postgrest_headers(service_role_key)

    select = (
        "id,taxpayer_id,location_number,change_type,previous_status,new_status,"
        "previous_permit_end_date,new_permit_end_date,detected_at,source_data_date,"
        "permit_location_id,"
        "comptroller_permit_locations(district_id,county,legal_name,location_name,"
        "address,city,state,zip,permit_start_date,permit_end_date,current_status)"
    )
    excluded = ",".join(NON_REVIEWABLE_CHANGE_TYPES)
    return _paginated_get(
        supabase_url,
        headers,
        "comptroller_permit_status_events",
        {
            "select": select,
            "change_type": f"not.in.({excluded})",
            "month_end_processed_at": "is.null",
            "detected_at": [f"gte.{start.isoformat()}", f"lt.{end_exclusive.isoformat()}"],
        },
        page_size=500,
    )


def _create_review(payload: dict[str, Any]) -> dict[str, Any] | None:
    supabase_url, service_role_key = get_supabase_config()
    headers = postgrest_headers(
        service_role_key,
        prefer="resolution=ignore-duplicates,return=representation",
    )
    rows = _request_json(
        "POST",
        f"{supabase_url}/rest/v1/comptroller_closure_reviews",
        headers,
        params={"on_conflict": "status_event_id"},
        json_payload=payload,
    )
    if isinstance(rows, list) and rows:
        return rows[0]
    return None


def _find_review_by_status_event_id(status_event_id: str) -> dict[str, Any] | None:
    """Look up an already-existing review for this event.

    _create_review returns None both when nothing was created (a conflict
    under resolution=ignore-duplicates) -- this recovers the review id in
    that case so a retry after an earlier partial run still backfills
    comptroller_permit_status_events.review_item_id instead of leaving it
    permanently null even though the review row exists.
    """

    supabase_url, service_role_key = get_supabase_config()
    headers = postgrest_headers(service_role_key)
    rows = _request_json(
        "GET",
        f"{supabase_url}/rest/v1/comptroller_closure_reviews",
        headers,
        params={"select": "id", "status_event_id": f"eq.{status_event_id}", "limit": "1"},
    )
    return rows[0] if rows else None


def _mark_event_processed(event_id: str, review_id: str | None) -> None:
    supabase_url, service_role_key = get_supabase_config()
    headers = postgrest_headers(service_role_key)
    payload = {"month_end_processed_at": datetime.now(timezone.utc).isoformat()}
    if review_id:
        payload["review_item_id"] = review_id
    _request_json(
        "PATCH",
        f"{supabase_url}/rest/v1/comptroller_permit_status_events",
        headers,
        params={"id": f"eq.{event_id}"},
        json_payload=payload,
    )


def process_month_end(target_month: date, *, dry_run: bool = False) -> MonthEndResult:
    events = get_unprocessed_events_for_month(target_month)

    matched_high = matched_medium = matched_low = unmatched = ambiguous_count = 0
    review_ids: list[str] = []

    # V1 has one monitored district, but a month-end batch can still contain
    # many events against it -- fetch each district's rendition-record list
    # at most once per run instead of once per event.
    candidates_by_district: dict[str, list[MatchCandidate]] = {}
    candidate_errors_by_district: dict[str, str] = {}

    run_id = None if dry_run else create_sync_run(RUN_TYPE_MONTH_END, "ALL")

    try:
        for event in events:
            location = event.get("comptroller_permit_locations") or {}
            district_id = location.get("district_id")

            if not district_id:
                match_result = match_closure_to_account(
                    district_id=None,
                    permit_legal_name=location.get("legal_name"),
                    permit_location_name=location.get("location_name"),
                )
            elif district_id in candidate_errors_by_district:
                match_result = MatchResult(
                    confidence="UNMATCHED",
                    score=0.0,
                    reason=candidate_errors_by_district[district_id],
                    candidate=None,
                )
            else:
                if district_id not in candidates_by_district:
                    try:
                        candidates_by_district[district_id] = fetch_candidate_records(district_id)
                    except MatchingConfigError as exc:
                        candidate_errors_by_district[district_id] = str(exc)
                if district_id in candidate_errors_by_district:
                    match_result = MatchResult(
                        confidence="UNMATCHED",
                        score=0.0,
                        reason=candidate_errors_by_district[district_id],
                        candidate=None,
                    )
                else:
                    match_result = match_closure_to_account(
                        district_id=district_id,
                        permit_legal_name=location.get("legal_name"),
                        permit_location_name=location.get("location_name"),
                        candidates=candidates_by_district[district_id],
                    )

            if match_result.confidence == "HIGH":
                matched_high += 1
            elif match_result.confidence == "MEDIUM":
                matched_medium += 1
            elif match_result.confidence == "LOW":
                matched_low += 1
            else:
                unmatched += 1
            if match_result.ambiguous:
                ambiguous_count += 1

            if dry_run:
                continue

            candidate = match_result.candidate
            review_payload = {
                "district_id": district_id,
                "permit_location_id": event.get("permit_location_id"),
                "status_event_id": event["id"],
                "review_month": target_month.isoformat(),
                "comptroller_taxpayer_id": event.get("taxpayer_id"),
                "comptroller_location_number": event.get("location_number"),
                "comptroller_business_name": location.get("location_name"),
                "comptroller_legal_name": location.get("legal_name"),
                "comptroller_address": location.get("address"),
                "comptroller_city": location.get("city"),
                "comptroller_state": location.get("state"),
                "comptroller_zip": location.get("zip"),
                "comptroller_permit_start_date": location.get("permit_start_date"),
                "comptroller_permit_end_date": event.get("new_permit_end_date") or location.get("permit_end_date"),
                "comptroller_previous_status": event.get("previous_status"),
                "comptroller_current_status": event.get("new_status"),
                "first_detected_at": event.get("detected_at"),
                "matched_account_id": candidate.record_id if candidate else None,
                "matched_account_number": candidate.account_number if candidate else None,
                "matched_owner_name": candidate.owner_name if candidate else None,
                "match_confidence": match_result.confidence,
                "match_score": match_result.score,
                "match_reason": match_result.reason,
                "match_ambiguous": match_result.ambiguous,
                "workflow_status": "PENDING_REVIEW",
            }
            review = _create_review(review_payload)
            if review is None:
                # resolution=ignore-duplicates: a review for this event already
                # exists from an earlier partial run. Look it up so the event
                # still gets its review_item_id backfilled below, instead of
                # marking the event processed with a permanently null link.
                review = _find_review_by_status_event_id(event["id"])
            review_id = review.get("id") if review else None
            if review_id:
                review_ids.append(review_id)
            _mark_event_processed(event["id"], review_id)

        if run_id:
            # permits_newly_inactive doesn't apply to month-end runs (that
            # metric belongs to daily syncs); leave it at its default so this
            # run's row doesn't imply every processed candidate was a new
            # inactivation. The matched/unmatched breakdown lives on the
            # comptroller_closure_reviews rows themselves, queryable by
            # review_month.
            finish_sync_run(
                run_id,
                status="SUCCESS",
                permits_checked=len(events),
            )
    except Exception as exc:  # noqa: BLE001
        if run_id:
            finish_sync_run(run_id, status="FAILED", error_message=str(exc))
        raise

    email_sent = False
    email_error: str | None = None
    if not dry_run:
        # Best-effort: the review data above is already correctly saved
        # regardless of whether this notification goes out, so a mail-server
        # hiccup must not make finish_sync_run(FAILED) fire for what was
        # otherwise a fully successful month-end run.
        month_label = f"{target_month:%Y-%m}"
        try:
            month_reviews = list_all_reviews_for_month(month_label)
            workbook_bytes = build_review_queue_workbook(month_reviews, month_label=month_label)
            send_month_end_export_email(month_label, workbook_bytes, review_count=len(month_reviews))
            email_sent = True
        except (EmailConfigError, EmailDeliveryError) as exc:
            email_error = str(exc)

    return MonthEndResult(
        review_month=target_month,
        dry_run=dry_run,
        candidates_processed=len(events),
        matched_high=matched_high,
        matched_medium=matched_medium,
        matched_low=matched_low,
        unmatched=unmatched,
        ambiguous=ambiguous_count,
        review_ids=review_ids,
        email_sent=email_sent,
        email_error=email_error,
    )
