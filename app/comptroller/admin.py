"""Read/update helpers backing the admin observability + review-queue endpoints.

Kept separate from service.py (sync/change-detection) and month_end.py
(batch processing) since this module is purely request-time reads/writes for
the FastAPI layer.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.comptroller.counties import get_monitored_counties
from app.comptroller.month_end import get_unprocessed_events_for_month, resolve_target_month
from app.comptroller.service import (
    ComptrollerServiceError,
    _request_json,
    get_supabase_config,
    postgrest_headers,
)

# Re-exported for backend/main.py and cli.py, which call it as
# `comptroller_admin.list_all_reviews_for_month(...)` -- it lives in
# service.py (not admin.py) to avoid a circular import with month_end.py.
from app.comptroller.service import list_all_reviews_for_month  # noqa: F401

REVIEW_WORKFLOW_STATUSES = (
    "PENDING_REVIEW",
    "CONFIRMED_CLOSURE",
    "NOT_CLOSED",
    "OWNERSHIP_CHANGE",
    "RELOCATED",
    "DUPLICATE",
    "OTHER_NEEDS_RESEARCH",
)


def _latest_sync_run(county: str, run_type: str | None = None) -> dict[str, Any] | None:
    supabase_url, service_role_key = get_supabase_config()
    headers = postgrest_headers(service_role_key)
    params: dict[str, Any] = {
        "select": "*",
        "county": f"eq.{county}",
        "order": "started_at.desc",
        "limit": "1",
    }
    if run_type:
        params["run_type"] = f"eq.{run_type}"
    rows = _request_json(
        "GET",
        f"{supabase_url}/rest/v1/comptroller_sync_runs",
        headers,
        params=params,
    )
    return rows[0] if rows else None


def get_sync_status_summary() -> dict[str, Any]:
    counties = get_monitored_counties()
    county_status = []
    for county in counties:
        county_status.append(
            {
                "county": county,
                "last_baseline_run": _latest_sync_run(county, "BASELINE"),
                "last_daily_run": _latest_sync_run(county, "DAILY"),
            }
        )

    last_month_end_run = _latest_sync_run("ALL", "MONTH_END")

    pending_month = resolve_target_month(None)
    try:
        pending_events = get_unprocessed_events_for_month(pending_month)
        pending_count = len(pending_events)
    except ComptrollerServiceError:
        pending_count = None

    return {
        "monitored_counties": counties,
        "county_status": county_status,
        "last_month_end_run": last_month_end_run,
        "pending_month_end": {
            "month": pending_month.isoformat()[:7],
            "unprocessed_event_count": pending_count,
        },
    }


def list_reviews(
    district_id: str,
    *,
    review_month: str | None = None,
    workflow_status: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    supabase_url, service_role_key = get_supabase_config()
    headers = postgrest_headers(service_role_key)
    params: dict[str, Any] = {
        "select": "*",
        "district_id": f"eq.{district_id}",
        "order": "created_at.desc",
        "limit": str(limit),
    }
    if review_month:
        params["review_month"] = f"eq.{review_month}-01"
    if workflow_status:
        params["workflow_status"] = f"eq.{workflow_status}"
    rows = _request_json(
        "GET",
        f"{supabase_url}/rest/v1/comptroller_closure_reviews",
        headers,
        params=params,
    )
    return rows if isinstance(rows, list) else []


def get_review_by_id(review_id: str) -> dict[str, Any] | None:
    supabase_url, service_role_key = get_supabase_config()
    headers = postgrest_headers(service_role_key)
    rows = _request_json(
        "GET",
        f"{supabase_url}/rest/v1/comptroller_closure_reviews",
        headers,
        params={"select": "*", "id": f"eq.{review_id}", "limit": "1"},
    )
    return rows[0] if rows else None


def update_review_workflow(
    review_id: str,
    *,
    workflow_status: str,
    reviewer_notes: str | None,
    reviewed_by: str | None,
) -> dict[str, Any]:
    """Update ONLY the review workflow status/notes/reviewer.

    This intentionally never touches property value, appraisal status,
    ownership, account status, BPP records, or exemption data -- those remain
    a human appraiser's decision, made through RenditionPilot's normal
    appraisal tools, not this monitoring feature.
    """

    if workflow_status not in REVIEW_WORKFLOW_STATUSES:
        raise ComptrollerServiceError(f"Unknown workflow_status '{workflow_status}'.")

    supabase_url, service_role_key = get_supabase_config()
    headers = postgrest_headers(service_role_key, prefer="return=representation")
    payload = {
        "workflow_status": workflow_status,
        "reviewer_notes": reviewer_notes,
        "reviewed_by": reviewed_by,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    }
    rows = _request_json(
        "PATCH",
        f"{supabase_url}/rest/v1/comptroller_closure_reviews",
        headers,
        params={"id": f"eq.{review_id}"},
        json_payload=payload,
    )
    if not rows:
        raise ComptrollerServiceError(f"Review {review_id} was not found.")
    return rows[0]
