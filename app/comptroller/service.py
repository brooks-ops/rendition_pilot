"""Supabase persistence + change detection for Comptroller permit monitoring.

Follows the same raw-PostgREST pattern as app/district_service.py: no ORM, a
module-level `_request_json` helper that tests monkeypatch, dataclasses for
typed results. See docs/comptroller_closure_monitor.md for the full design.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

import requests

from app.comptroller import client as comptroller_client
from app.comptroller.counties import get_county_code, get_district_slug_for_county

DEFAULT_MIN_EXPECTED_ROW_RATIO = 0.5
UPSERT_CHUNK_SIZE = 500

RUN_TYPE_BASELINE = "BASELINE"
RUN_TYPE_DAILY = "DAILY"
RUN_TYPE_MONTH_END = "MONTH_END"
RUN_TYPE_MANUAL = "MANUAL"


class ComptrollerServiceError(RuntimeError):
    pass


@dataclass(frozen=True)
class SyncRunResult:
    run_id: str
    run_type: str
    county: str
    status: str
    permits_checked: int
    permits_new: int
    permits_newly_inactive: int
    error_message: str | None
    source_data_date: datetime | None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_supabase_config() -> tuple[str, str]:
    supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    service_role_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    if not supabase_url:
        raise ComptrollerServiceError("SUPABASE_URL is not configured.")
    if not service_role_key:
        raise ComptrollerServiceError("SUPABASE_SERVICE_ROLE_KEY is required for Comptroller sync.")
    return supabase_url, service_role_key


def postgrest_headers(service_role_key: str, *, prefer: str | None = None) -> dict[str, str]:
    headers = {
        "apikey": service_role_key,
        "Authorization": f"Bearer {service_role_key}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def _extract_error_message(payload: Any) -> str:
    if isinstance(payload, dict):
        return str(payload.get("message") or payload.get("error") or payload)
    return str(payload)


def _request_json(
    method: str,
    url: str,
    headers: dict[str, str],
    *,
    params: dict[str, Any] | None = None,
    json_payload: Any = None,
) -> Any:
    response = requests.request(
        method=method,
        url=url,
        headers=headers,
        params=params,
        json=json_payload,
        timeout=20,
    )
    try:
        payload = response.json()
    except ValueError:
        payload = response.text if response.text else None

    if response.status_code >= 400:
        message = _extract_error_message(payload)
        if "comptroller_" in message.lower() and (
            "does not exist" in message.lower() or "schema cache" in message.lower()
        ):
            message = (
                f"{message} Run "
                "supabase/migrations/20260818_comptroller_closure_monitor.sql in the Supabase SQL editor."
            )
        raise ComptrollerServiceError(message)
    return payload


def _paginated_get(
    supabase_url: str,
    headers: dict[str, str],
    table: str,
    params: dict[str, Any],
    *,
    page_size: int = 1000,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        page_params = dict(params)
        page_params["limit"] = page_size
        page_params["offset"] = offset
        page = _request_json(
            "GET",
            f"{supabase_url}/rest/v1/{table}",
            headers,
            params=page_params,
        )
        if not isinstance(page, list):
            raise ComptrollerServiceError(f"Unexpected response fetching {table}: {page!r}")
        rows.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
    return rows


def get_existing_locations_by_key(county_name: str) -> dict[tuple[str, str], dict[str, Any]]:
    supabase_url, service_role_key = get_supabase_config()
    headers = postgrest_headers(service_role_key)
    rows = _paginated_get(
        supabase_url,
        headers,
        "comptroller_permit_locations",
        {
            "select": "id,taxpayer_id,location_number,current_status,permit_end_date,first_seen_at,is_baseline,last_changed_at",
            "county": f"eq.{county_name}",
        },
    )
    return {(row["taxpayer_id"], row["location_number"]): row for row in rows}


def get_last_successful_checked_count(county_name: str) -> int | None:
    supabase_url, service_role_key = get_supabase_config()
    headers = postgrest_headers(service_role_key)
    rows = _request_json(
        "GET",
        f"{supabase_url}/rest/v1/comptroller_sync_runs",
        headers,
        params={
            "select": "permits_checked",
            "county": f"eq.{county_name}",
            "status": "eq.SUCCESS",
            "order": "started_at.desc",
            "limit": "1",
        },
    )
    if not rows:
        return None
    return int(rows[0].get("permits_checked") or 0)


def resolve_district_id(county_name: str) -> str | None:
    supabase_url, service_role_key = get_supabase_config()
    headers = postgrest_headers(service_role_key)
    slug = get_district_slug_for_county(county_name)
    rows = _request_json(
        "GET",
        f"{supabase_url}/rest/v1/districts",
        headers,
        params={"select": "id", "slug": f"eq.{slug}", "limit": "1"},
    )
    if not rows:
        return None
    return rows[0].get("id")


def create_sync_run(run_type: str, county_name: str) -> str:
    supabase_url, service_role_key = get_supabase_config()
    headers = postgrest_headers(service_role_key, prefer="return=representation")
    rows = _request_json(
        "POST",
        f"{supabase_url}/rest/v1/comptroller_sync_runs",
        headers,
        json_payload={"run_type": run_type, "county": county_name, "status": "RUNNING"},
    )
    row = rows[0] if isinstance(rows, list) else rows
    run_id = row.get("id")
    if not run_id:
        raise ComptrollerServiceError("Failed to create a comptroller_sync_runs row.")
    return run_id


def finish_sync_run(
    run_id: str,
    *,
    status: str,
    permits_checked: int = 0,
    permits_new: int = 0,
    permits_newly_inactive: int = 0,
    error_message: str | None = None,
    source_data_date: datetime | None = None,
) -> None:
    supabase_url, service_role_key = get_supabase_config()
    headers = postgrest_headers(service_role_key)
    payload: dict[str, Any] = {
        "status": status,
        "permits_checked": permits_checked,
        "permits_new": permits_new,
        "permits_newly_inactive": permits_newly_inactive,
        "error_message": error_message,
        "finished_at": _now_iso(),
    }
    if source_data_date is not None:
        payload["source_data_date"] = source_data_date.isoformat()
    _request_json(
        "PATCH",
        f"{supabase_url}/rest/v1/comptroller_sync_runs",
        headers,
        params={"id": f"eq.{run_id}"},
        json_payload=payload,
    )


def upsert_permit_locations(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows:
        return []
    supabase_url, service_role_key = get_supabase_config()
    headers = postgrest_headers(
        service_role_key,
        prefer="resolution=merge-duplicates,return=representation",
    )
    results: list[dict[str, Any]] = []
    for start in range(0, len(rows), UPSERT_CHUNK_SIZE):
        chunk = rows[start : start + UPSERT_CHUNK_SIZE]
        response_rows = _request_json(
            "POST",
            f"{supabase_url}/rest/v1/comptroller_permit_locations",
            headers,
            params={"on_conflict": "taxpayer_id,location_number"},
            json_payload=chunk,
        )
        if isinstance(response_rows, list):
            results.extend(response_rows)
    return results


def insert_status_events(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    supabase_url, service_role_key = get_supabase_config()
    headers = postgrest_headers(service_role_key)
    for start in range(0, len(rows), UPSERT_CHUNK_SIZE):
        chunk = rows[start : start + UPSERT_CHUNK_SIZE]
        _request_json(
            "POST",
            f"{supabase_url}/rest/v1/comptroller_permit_status_events",
            headers,
            json_payload=chunk,
        )


def list_all_reviews_for_month(review_month: str, *, district_id: str | None = None) -> list[dict[str, Any]]:
    """Every review for a month, fully paginated (no row cap) -- for export.

    Lives here (not admin.py) so month_end.py can call it too without a
    circular import (admin.py imports from month_end.py for other reasons).

    `district_id`, when given, scopes the result to one district (used by the
    web API export endpoint, so a district admin can only export their own
    district's data). The CLI export command and the automatic month-end
    email omit it to cover every monitored district.
    """

    supabase_url, service_role_key = get_supabase_config()
    headers = postgrest_headers(service_role_key)
    params: dict[str, Any] = {
        "select": "*",
        "review_month": f"eq.{review_month}-01",
        "order": "created_at.asc",
    }
    if district_id:
        params["district_id"] = f"eq.{district_id}"
    return _paginated_get(supabase_url, headers, "comptroller_closure_reviews", params, page_size=500)


def has_unprocessed_duplicate_event(
    permit_location_id: str,
    new_status: str,
    new_permit_end_date: str | None,
) -> bool:
    """True if an unprocessed, non-BASELINE event already records this exact new state.

    Guards against duplicate events if a sync crashes after writing a status
    event but before the corresponding comptroller_permit_locations upsert
    (see sync_county's write order): a retry would independently re-detect
    the same change against the still-stale location row and would otherwise
    insert a second, identical event for it.

    change_type=not.in.(BASELINE) matters: a permit that was already INACTIVE
    at baseline time has a BASELINE event whose new_status/new_permit_end_date
    are (by definition) identical to its "current" state, and that row is
    never marked month_end_processed_at. Without excluding it here, any later
    re-detection landing back on that same state (e.g. a stale/corrected
    `prior` row) would be wrongly treated as a duplicate of the baseline
    snapshot and silently dropped -- confirmed live against production data
    on 2026-08-21.
    """

    supabase_url, service_role_key = get_supabase_config()
    headers = postgrest_headers(service_role_key)
    params: dict[str, Any] = {
        "select": "id",
        "permit_location_id": f"eq.{permit_location_id}",
        "new_status": f"eq.{new_status}",
        "change_type": "not.in.(BASELINE)",
        "month_end_processed_at": "is.null",
        "limit": "1",
    }
    params["new_permit_end_date"] = "is.null" if new_permit_end_date is None else f"eq.{new_permit_end_date}"
    rows = _request_json(
        "GET",
        f"{supabase_url}/rest/v1/comptroller_permit_status_events",
        headers,
        params=params,
    )
    return bool(rows)


def _date_or_none(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def detect_change(prior_row: dict[str, Any], record: comptroller_client.PermitRecord) -> tuple[bool, str | None]:
    """Compare stored state against a freshly fetched record.

    Returns (changed, change_type). Exactly one change_type is returned per
    call so a single source update that both flips status and adds an end
    date is reported as ONE event (STATUS_AND_END_DATE_CHANGE), never two.
    """

    old_status = str(prior_row.get("current_status") or "ACTIVE")
    old_end = _date_or_none(prior_row.get("permit_end_date"))
    new_status = record.current_status
    new_end = record.permit_end_date

    status_flipped_to_inactive = old_status == "ACTIVE" and new_status == "INACTIVE"
    end_date_newly_added = old_end is None and new_end is not None
    reopened = old_status == "INACTIVE" and new_status == "ACTIVE"

    if status_flipped_to_inactive and end_date_newly_added:
        return True, "STATUS_AND_END_DATE_CHANGE"
    if status_flipped_to_inactive:
        return True, "STATUS_CHANGE"
    if end_date_newly_added:
        return True, "PERMIT_END_DATE_ADDED"
    if reopened:
        return True, "REOPENED"
    if old_end is not None and new_end is not None and old_end != new_end:
        return True, "OTHER"
    return False, None


def _build_location_payload(
    record: comptroller_client.PermitRecord,
    *,
    county_name: str,
    district_id: str | None,
    dataset_id: str,
    prior: dict[str, Any] | None,
    is_baseline_run: bool,
    changed: bool,
) -> dict[str, Any]:
    """Build one row for the bulk comptroller_permit_locations upsert.

    Every payload in a single upsert_permit_locations() call MUST have the
    exact same set of keys -- PostgREST's bulk insert rejects a batch with
    heterogeneous object shapes ("All object keys must match", confirmed
    live against production on 2026-08-21 when this function used to
    conditionally omit first_seen_at/is_baseline/last_changed_at for some
    rows in a batch but not others). So every key below is always present;
    for an existing row, a field that shouldn't be touched this sync is set
    back to its current stored value (via `prior`) rather than omitted or
    zeroed out, so a merge-duplicates upsert can't silently erase history.
    """
    now = _now_iso()
    is_new_row = prior is None
    return {
        "taxpayer_id": record.taxpayer_id,
        "location_number": record.location_number,
        "county": county_name,
        "legal_name": record.legal_name,
        "location_name": record.location_name,
        "address": record.address,
        "city": record.city,
        "state": record.state,
        "zip": record.zip,
        "permit_start_date": record.permit_start_date.isoformat() if record.permit_start_date else None,
        "permit_end_date": record.permit_end_date.isoformat() if record.permit_end_date else None,
        "current_status": record.current_status,
        "source": "tx_comptroller_open_data",
        "source_dataset_id": dataset_id,
        "source_row_raw": record.raw,
        "last_checked_at": now,
        # Only overwrite district_id when we actually resolved one this run;
        # otherwise keep whatever an existing row already had (a transient
        # district-lookup failure shouldn't blank out a previously-known value).
        "district_id": district_id if district_id else (prior.get("district_id") if prior else None),
        "first_seen_at": now if is_new_row else prior.get("first_seen_at"),
        "is_baseline": is_baseline_run if is_new_row else bool(prior.get("is_baseline")),
        "last_changed_at": now if changed else (None if is_new_row else prior.get("last_changed_at")),
    }


def sync_county(
    county_name: str,
    *,
    requested_run_type: str = RUN_TYPE_DAILY,
    min_expected_row_ratio: float | None = None,
) -> SyncRunResult:
    """Fetch, upsert, and diff one monitored county's permit universe.

    Safe to call repeatedly: a county with no prior rows is automatically
    treated as its baseline import (change_type='BASELINE', no closure
    events), and every subsequent call only records events for meaningful
    status/permit-end-date changes. Never mutates comptroller_permit_locations
    if the fetch fails or looks like a partial download.
    """

    county_code = get_county_code(county_name)
    if not county_code:
        raise ComptrollerServiceError(
            f"'{county_name}' has no known Comptroller county code. "
            "Add it to app/comptroller/counties.py TEXAS_COUNTY_CODES first."
        )

    ratio = (
        min_expected_row_ratio
        if min_expected_row_ratio is not None
        else float(os.getenv("COMPTROLLER_MIN_EXPECTED_ROW_RATIO", str(DEFAULT_MIN_EXPECTED_ROW_RATIO)))
    )

    existing = get_existing_locations_by_key(county_name)
    is_baseline_run = len(existing) == 0
    effective_run_type = RUN_TYPE_BASELINE if is_baseline_run else requested_run_type

    run_id = create_sync_run(effective_run_type, county_name)

    try:
        if not is_baseline_run:
            previous_checked = get_last_successful_checked_count(county_name)
            if previous_checked:
                fetch_preview_threshold = previous_checked * ratio
            else:
                fetch_preview_threshold = 0
        else:
            fetch_preview_threshold = 0

        fetch_result = comptroller_client.fetch_county_permits(county_code)

        if not is_baseline_run and fetch_preview_threshold and len(fetch_result.records) < fetch_preview_threshold:
            raise ComptrollerServiceError(
                f"Comptroller fetch for {county_name} returned only {len(fetch_result.records)} records, "
                f"below the expected minimum of {fetch_preview_threshold:.0f} "
                f"(based on the last successful run). Treating this as a partial/failed download and "
                "leaving existing permit data untouched."
            )

        district_id = None
        try:
            district_id = resolve_district_id(county_name)
        except ComptrollerServiceError:
            district_id = None

        location_payloads: list[dict[str, Any]] = []
        baseline_events_pending_id: list[dict[str, Any]] = []
        existing_row_events: list[dict[str, Any]] = []
        new_count = 0
        newly_inactive_count = 0

        for record in fetch_result.records:
            prior = existing.get(record.key)
            is_new_row = prior is None

            changed, change_type = (False, None) if is_new_row else detect_change(prior, record)

            location_payloads.append(
                _build_location_payload(
                    record,
                    county_name=county_name,
                    district_id=district_id,
                    dataset_id=fetch_result.dataset_id,
                    prior=prior,
                    is_baseline_run=is_baseline_run,
                    changed=changed,
                )
            )

            if is_new_row:
                new_count += 1
                if is_baseline_run:
                    baseline_events_pending_id.append(
                        {
                            "taxpayer_id": record.taxpayer_id,
                            "location_number": record.location_number,
                            "change_type": "BASELINE",
                            "previous_status": None,
                            "new_status": record.current_status,
                            "previous_permit_end_date": None,
                            "new_permit_end_date": (
                                record.permit_end_date.isoformat() if record.permit_end_date else None
                            ),
                            "source_data_date": (
                                fetch_result.source_data_date.isoformat() if fetch_result.source_data_date else None
                            ),
                            "sync_run_id": run_id,
                        }
                    )
                # A brand-new permit discovered on a non-baseline run has no
                # prior state to diff against; first_seen_at on the location
                # row is sufficient per spec, no status event is recorded.
                continue

            if not changed:
                continue

            if change_type in ("STATUS_CHANGE", "STATUS_AND_END_DATE_CHANGE", "PERMIT_END_DATE_ADDED"):
                newly_inactive_count += 1

            new_permit_end_date = record.permit_end_date.isoformat() if record.permit_end_date else None
            # prior["id"] already exists (this row is not new), so this event
            # can be written now, before comptroller_permit_locations is
            # upserted below -- see has_unprocessed_duplicate_event for why
            # that ordering matters for crash-safety.
            existing_row_events.append(
                {
                    "permit_location_id": prior["id"],
                    "taxpayer_id": record.taxpayer_id,
                    "location_number": record.location_number,
                    "change_type": change_type,
                    "previous_status": prior.get("current_status"),
                    "new_status": record.current_status,
                    "previous_permit_end_date": prior.get("permit_end_date"),
                    "new_permit_end_date": new_permit_end_date,
                    "source_data_date": (
                        fetch_result.source_data_date.isoformat() if fetch_result.source_data_date else None
                    ),
                    "sync_run_id": run_id,
                }
            )

        # Write existing-row events BEFORE flipping their location's status:
        # if this process crashes between the two steps, a retry will still
        # see the OLD status on comptroller_permit_locations, re-detect the
        # same change, and try to write the same event again -- the dedup
        # check below turns that into a no-op instead of a duplicate. Writing
        # events after the upsert (the alternative ordering) would instead
        # risk *silently losing* the event forever, since a retry would then
        # see the already-updated status and detect no change at all.
        events_to_insert = [
            event
            for event in existing_row_events
            if not has_unprocessed_duplicate_event(
                event["permit_location_id"], event["new_status"], event["new_permit_end_date"]
            )
        ]
        insert_status_events(events_to_insert)

        upserted_rows = upsert_permit_locations(location_payloads)

        if baseline_events_pending_id:
            id_by_key = {(row["taxpayer_id"], row["location_number"]): row["id"] for row in upserted_rows}
            baseline_events_to_insert = []
            for event in baseline_events_pending_id:
                key = (event["taxpayer_id"], event["location_number"])
                location_id = id_by_key.get(key)
                if not location_id:
                    continue
                baseline_events_to_insert.append({**event, "permit_location_id": location_id})
            insert_status_events(baseline_events_to_insert)

        finish_sync_run(
            run_id,
            status="SUCCESS",
            permits_checked=len(fetch_result.records),
            permits_new=new_count,
            permits_newly_inactive=newly_inactive_count,
            source_data_date=fetch_result.source_data_date,
        )

        return SyncRunResult(
            run_id=run_id,
            run_type=effective_run_type,
            county=county_name,
            status="SUCCESS",
            permits_checked=len(fetch_result.records),
            permits_new=new_count,
            permits_newly_inactive=newly_inactive_count,
            error_message=None,
            source_data_date=fetch_result.source_data_date,
        )
    except Exception as exc:  # noqa: BLE001 - must always record the failure before propagating
        finish_sync_run(run_id, status="FAILED", error_message=str(exc))
        raise
