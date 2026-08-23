"""Persists a locked rendition review into the pipeline-shaped tables
(`rendition_uploads` -> `rendition_jobs` -> `parsed_rendition_results`) that
already exist in production but, until this module, had no writer anywhere
in the codebase -- confirmed by tracing every `/api/review/*` endpoint and
every Supabase REST write call site in the app; the browser held the OCR
result in memory for the whole run/lock/save sequence and nothing server-
side ever persisted it. See docs/rendition_persistence.md.

These three tables are NOT created by any migration tracked in this repo --
they exist in production out-of-band. Their live shape was confirmed via
PostgREST's own OpenAPI introspection (`GET {SUPABASE_URL}/rest/v1/` with
`Accept: application/openapi+json`) on 2026-08-22, not assumed from a
migration file. This module writes only within that confirmed shape and
never attempts to alter it.

Called from `POST /api/review/lock` -- the point where an appraiser has
confirmed a final value and a BPP account number, not from `/api/review/run`
(which persisting would mean writing one row per OCR attempt, including
ones a reviewer immediately discards). Only ever writes when a
server-verified `district_id` is available (see backend/main.py's
`get_authenticated_district_context`) -- never trusts a client-supplied
district value for a write, unlike the merely-cosmetic `district_context`
field `build_final_review_record` already accepted.

Dedup: one row per (district_id, tax_year, account_number). Re-locking the
same account/year updates that row in place (last-locked-wins) rather than
creating a duplicate; a different tax_year for the same account always gets
its own row, never overwriting prior-year history. No unique DB constraint
exists on this out-of-band table to upsert against, so this is an
application-level check-then-write -- acceptable given this is a
low-concurrency, one-appraiser-at-a-time workflow, not a bulk pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.comptroller.service import _request_json, get_supabase_config, postgrest_headers


class RenditionPersistenceError(RuntimeError):
    pass


@dataclass(frozen=True)
class PersistedRenditionResult:
    parsed_rendition_result_id: str
    upload_id: str
    job_id: str
    created: bool  # True if a new row was created, False if an existing one was updated


def _headers(prefer: str | None = None) -> dict[str, str]:
    _, service_role_key = get_supabase_config()
    return postgrest_headers(service_role_key, prefer=prefer)


def _base_url() -> str:
    supabase_url, _ = get_supabase_config()
    return supabase_url


def _parse_tax_year(metadata: dict[str, Any]) -> int | None:
    raw = metadata.get("tax_year")
    try:
        return int(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _parse_numeric(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _find_existing_result(district_id: str, tax_year: int, account_number: str) -> dict[str, Any] | None:
    rows = _request_json(
        "GET",
        f"{_base_url()}/rest/v1/parsed_rendition_results",
        _headers(),
        params={
            "select": "id,upload_id,job_id",
            "district_id": f"eq.{district_id}",
            "tax_year": f"eq.{tax_year}",
            "result->metadata->>account_number": f"eq.{account_number}",
            "limit": "1",
        },
    )
    return rows[0] if isinstance(rows, list) and rows else None


def _create_upload(district_id: str, tax_year: int | None, file_name: str, created_by: str | None) -> str:
    rows = _request_json(
        "POST",
        f"{_base_url()}/rest/v1/rendition_uploads",
        _headers(prefer="return=representation"),
        json_payload=[{
            "district_id": district_id,
            "tax_year": tax_year,
            "file_name": file_name,
            "original_filename": file_name,
            "status": "reviewed",
            "created_by": created_by,
        }],
    )
    if not isinstance(rows, list) or not rows:
        raise RenditionPersistenceError(f"Unexpected response creating rendition_uploads row: {rows!r}")
    return rows[0]["id"]


def _create_job(district_id: str, tax_year: int | None, upload_id: str, result: dict[str, Any], created_by: str | None) -> str:
    rows = _request_json(
        "POST",
        f"{_base_url()}/rest/v1/rendition_jobs",
        _headers(prefer="return=representation"),
        json_payload=[{
            "district_id": district_id,
            "tax_year": tax_year,
            "upload_id": upload_id,
            "status": "completed",
            "result": result,
            "created_by": created_by,
        }],
    )
    if not isinstance(rows, list) or not rows:
        raise RenditionPersistenceError(f"Unexpected response creating rendition_jobs row: {rows!r}")
    return rows[0]["id"]


def persist_locked_review(
    *,
    district_id: str,
    file_name: str,
    result: dict[str, Any],
    account_number: str,
    final_value: float | None,
    pipeline_confidence: Any = None,
    created_by: str | None = None,
) -> PersistedRenditionResult:
    """Persists one locked review. `account_number` must already be the
    appraiser-confirmed value (normalized/validated by the caller, exactly
    like `/api/review/lock` already requires before calling this) -- it
    overwrites whatever `result.metadata.account_number` the OCR extracted,
    since the confirmed value is what future matching should trust (see
    docs/rendition_persistence.md's account-number-semantics section)."""

    if not account_number:
        raise RenditionPersistenceError("account_number is required to persist a rendition result.")

    metadata = dict(result.get("metadata") or {})
    metadata["account_number"] = account_number
    stored_result = {**result, "metadata": metadata}
    tax_year = _parse_tax_year(metadata)
    confidence = _parse_numeric(pipeline_confidence)

    existing = _find_existing_result(district_id, tax_year, account_number) if tax_year is not None else None

    if existing is not None:
        _request_json(
            "PATCH",
            f"{_base_url()}/rest/v1/parsed_rendition_results",
            _headers(),
            params={"id": f"eq.{existing['id']}"},
            json_payload={
                "result": stored_result,
                "recommended_value": final_value,
                "confidence": confidence,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return PersistedRenditionResult(
            parsed_rendition_result_id=existing["id"],
            upload_id=existing["upload_id"],
            job_id=existing["job_id"],
            created=False,
        )

    upload_id = _create_upload(district_id, tax_year, file_name, created_by)
    job_id = _create_job(district_id, tax_year, upload_id, stored_result, created_by)
    rows = _request_json(
        "POST",
        f"{_base_url()}/rest/v1/parsed_rendition_results",
        _headers(prefer="return=representation"),
        json_payload=[{
            "district_id": district_id,
            "tax_year": tax_year,
            "upload_id": upload_id,
            "job_id": job_id,
            "result": stored_result,
            "recommended_value": final_value,
            "confidence": confidence,
            "created_by": created_by,
        }],
    )
    if not isinstance(rows, list) or not rows:
        raise RenditionPersistenceError(f"Unexpected response creating parsed_rendition_results row: {rows!r}")
    return PersistedRenditionResult(
        parsed_rendition_result_id=rows[0]["id"], upload_id=upload_id, job_id=job_id, created=True,
    )
