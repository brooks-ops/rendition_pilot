"""Unified BPP Intelligence Queue: read-time merge of `bpp_intelligence_items`
(New Business Detection today; future signal types plug in here) with the
existing sales-tax closure monitor's `comptroller_closure_reviews` table.

WHY A READ-TIME MERGE, NOT A SCHEMA MIGRATION: comptroller_closure_reviews is
the live, already-running sales-tax closure monitor's data. Its write path
(app/comptroller/month_end.py) is deliberately left untouched -- migrating it
onto bpp_intelligence_items would mean moving production rows and rewriting
a tested, deployed, cron-scheduled write path for an architectural win, with
real risk to a working feature. Instead: fetch from both tables, normalize
into one `UnifiedIntelligenceItem` shape for display, and route write
actions (investigate/resolve/dismiss) back to whichever table the item
actually lives in via `source_table`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.comptroller import admin as comptroller_admin
from app.comptroller.service import _request_json, get_supabase_config, postgrest_headers

SOURCE_TABLE_INTELLIGENCE = "bpp_intelligence_items"
SOURCE_TABLE_CLOSURE_REVIEW = "comptroller_closure_reviews"

# comptroller_closure_reviews.workflow_status -> the shared 4-state lifecycle,
# for display/filtering only. The real value is preserved as `resolution`.
_CLOSURE_STATUS_MAP = {
    "PENDING_REVIEW": "NEW",
    "OTHER_NEEDS_RESEARCH": "IN_REVIEW",
    "CONFIRMED_CLOSURE": "RESOLVED",
    "OWNERSHIP_CHANGE": "RESOLVED",
    "RELOCATED": "RESOLVED",
    "NOT_CLOSED": "DISMISSED",
    "DUPLICATE": "DISMISSED",
}

# Resolution (shared vocabulary) -> comptroller_closure_reviews.workflow_status,
# for routing a resolve/dismiss action back onto that table's own vocabulary.
_RESOLUTION_TO_CLOSURE_WORKFLOW = {
    "ACCOUNT_EXISTS": "NOT_CLOSED",
    "BUSINESS_CLOSED": "CONFIRMED_CLOSURE",
    "RELOCATION": "RELOCATED",
    "OWNERSHIP_CHANGE": "OWNERSHIP_CHANGE",
    "DUPLICATE": "DUPLICATE",
    "FALSE_MATCH": "NOT_CLOSED",
    "OTHER": "OTHER_NEEDS_RESEARCH",
}

RESOLUTION_OPTIONS_BY_SIGNAL_TYPE = {
    "new_business": [
        "ACCOUNT_EXISTS",
        "NEW_ACCOUNT_NEEDED",
        "OWNERSHIP_CHANGE",
        "RELOCATION",
        "DUPLICATE",
        "NO_TAXABLE_BPP",
        "FALSE_MATCH",
        "OTHER",
    ],
    "sales_tax_inactive": [
        "ACCOUNT_EXISTS",
        "BUSINESS_CLOSED",
        "OWNERSHIP_CHANGE",
        "RELOCATION",
        "DUPLICATE",
        "FALSE_MATCH",
        "OTHER",
    ],
}


class IntelligenceQueueError(RuntimeError):
    pass


@dataclass(frozen=True)
class UnifiedIntelligenceItem:
    id: str
    source_table: str
    signal_type: str
    status: str
    classification: str | None
    priority: str
    confidence: str | None
    confidence_score: float | None
    is_ambiguous: bool
    business_name: str | None
    legal_name: str | None
    source_address: str | None
    source_city: str | None
    source_state: str | None
    source_zip: str | None
    permit_start_date: str | None
    permit_end_date: str | None
    first_detected_at: str | None
    matched_account_number: str | None
    matched_owner_name: str | None
    match_reason: str | None
    match_signals: dict[str, Any] | None
    recommended_action: str | None
    resolution: str | None
    resolution_notes: str | None
    reviewed_by: str | None
    reviewed_at: str | None
    district_id: str | None
    jurisdiction_id: str | None
    created_at: str | None
    raw: dict[str, Any]
    # Property Enrichment evidence (see app/comptroller/property_enrichment.py)
    # -- always None for closure-review rows and for jurisdictions with no
    # property data loaded, never fabricated. Defaulted so existing
    # construction sites (tests, callers) don't need updating for an
    # additive field.
    property_match_status: str | None = None
    matched_address: str | None = None
    property_account_number: str | None = None
    tug: str | None = None
    neighborhood: str | None = None
    map_id: str | None = None


def _from_intelligence_row(row: dict[str, Any]) -> UnifiedIntelligenceItem:
    return UnifiedIntelligenceItem(
        id=row["id"],
        source_table=SOURCE_TABLE_INTELLIGENCE,
        signal_type=row.get("signal_type", "new_business"),
        status=row.get("status", "NEW"),
        classification=row.get("classification"),
        priority=row.get("priority", "MEDIUM"),
        confidence=row.get("confidence"),
        confidence_score=row.get("confidence_score"),
        is_ambiguous=bool(row.get("is_ambiguous")),
        business_name=row.get("business_name"),
        legal_name=row.get("legal_name"),
        source_address=row.get("source_address"),
        source_city=row.get("source_city"),
        source_state=row.get("source_state"),
        source_zip=row.get("source_zip"),
        permit_start_date=row.get("permit_start_date"),
        permit_end_date=row.get("permit_end_date"),
        first_detected_at=row.get("first_detected_at"),
        matched_account_number=row.get("matched_account_number"),
        matched_owner_name=row.get("matched_owner_name"),
        match_reason=row.get("match_reason"),
        match_signals=row.get("match_signals"),
        recommended_action=row.get("recommended_action"),
        resolution=row.get("resolution"),
        resolution_notes=row.get("resolution_notes"),
        reviewed_by=row.get("reviewed_by"),
        reviewed_at=row.get("reviewed_at"),
        district_id=row.get("district_id"),
        jurisdiction_id=row.get("jurisdiction_id"),
        created_at=row.get("created_at"),
        property_match_status=row.get("property_match_status"),
        matched_address=row.get("matched_address"),
        property_account_number=row.get("property_account_number"),
        tug=row.get("tug"),
        neighborhood=row.get("neighborhood"),
        map_id=row.get("map_id"),
        raw=row,
    )


def _from_closure_review_row(row: dict[str, Any]) -> UnifiedIntelligenceItem:
    workflow_status = row.get("workflow_status", "PENDING_REVIEW")
    return UnifiedIntelligenceItem(
        id=row["id"],
        source_table=SOURCE_TABLE_CLOSURE_REVIEW,
        signal_type="sales_tax_inactive",
        status=_CLOSURE_STATUS_MAP.get(workflow_status, "NEW"),
        classification=None,
        priority="MEDIUM",
        confidence=row.get("match_confidence"),
        confidence_score=row.get("match_score"),
        is_ambiguous=bool(row.get("match_ambiguous")),
        business_name=row.get("comptroller_business_name"),
        legal_name=row.get("comptroller_legal_name"),
        source_address=row.get("comptroller_address"),
        source_city=row.get("comptroller_city"),
        source_state=row.get("comptroller_state"),
        source_zip=row.get("comptroller_zip"),
        permit_start_date=row.get("comptroller_permit_start_date"),
        permit_end_date=row.get("comptroller_permit_end_date"),
        first_detected_at=row.get("first_detected_at"),
        matched_account_number=row.get("matched_account_number"),
        matched_owner_name=row.get("matched_owner_name"),
        match_reason=row.get("match_reason"),
        match_signals=None,
        recommended_action="Review for possible business closure.",
        resolution=workflow_status,
        resolution_notes=row.get("reviewer_notes"),
        reviewed_by=row.get("reviewed_by"),
        reviewed_at=row.get("reviewed_at"),
        district_id=row.get("district_id"),
        jurisdiction_id=None,
        created_at=row.get("created_at"),
        property_match_status=None,
        matched_address=row.get("matched_situs_address"),
        property_account_number=None,
        tug=None,
        neighborhood=None,
        map_id=None,
        raw=row,
    )


def _fetch_intelligence_items(district_id: str, *, signal_type: str | None) -> list[dict[str, Any]]:
    supabase_url, service_role_key = get_supabase_config()
    headers = postgrest_headers(service_role_key)
    params: dict[str, Any] = {"select": "*", "district_id": f"eq.{district_id}", "order": "created_at.desc", "limit": "500"}
    if signal_type:
        params["signal_type"] = f"eq.{signal_type}"
    rows = _request_json("GET", f"{supabase_url}/rest/v1/bpp_intelligence_items", headers, params=params)
    return rows if isinstance(rows, list) else []


def _fetch_closure_reviews(district_id: str) -> list[dict[str, Any]]:
    supabase_url, service_role_key = get_supabase_config()
    headers = postgrest_headers(service_role_key)
    rows = _request_json(
        "GET",
        f"{supabase_url}/rest/v1/comptroller_closure_reviews",
        headers,
        params={"select": "*", "district_id": f"eq.{district_id}", "order": "created_at.desc", "limit": "500"},
    )
    return rows if isinstance(rows, list) else []


def list_intelligence_queue(
    district_id: str,
    *,
    signal_type: str | None = None,
    status: str | None = None,
    confidence: str | None = None,
    city: str | None = None,
) -> list[UnifiedIntelligenceItem]:
    items: list[UnifiedIntelligenceItem] = []

    if signal_type in (None, "new_business"):
        items.extend(_from_intelligence_row(row) for row in _fetch_intelligence_items(district_id, signal_type=signal_type))
    if signal_type in (None, "sales_tax_inactive"):
        items.extend(_from_closure_review_row(row) for row in _fetch_closure_reviews(district_id))

    if status:
        items = [i for i in items if i.status == status]
    if confidence:
        items = [i for i in items if i.confidence == confidence]
    if city:
        items = [i for i in items if (i.source_city or "").strip().upper() == city.strip().upper()]

    items.sort(key=lambda i: i.created_at or "", reverse=True)
    return items


def get_queue_summary(district_id: str) -> dict[str, int]:
    items = list_intelligence_queue(district_id)
    return {
        "new": sum(1 for i in items if i.status == "NEW"),
        "high_priority": sum(1 for i in items if i.priority == "HIGH" and i.status in ("NEW", "IN_REVIEW")),
        "needs_review": sum(1 for i in items if i.status == "IN_REVIEW"),
        "resolved": sum(1 for i in items if i.status in ("RESOLVED", "DISMISSED")),
        "total": len(items),
    }


def get_intelligence_item(source_table: str, item_id: str) -> UnifiedIntelligenceItem | None:
    supabase_url, service_role_key = get_supabase_config()
    headers = postgrest_headers(service_role_key)
    if source_table == SOURCE_TABLE_INTELLIGENCE:
        rows = _request_json(
            "GET", f"{supabase_url}/rest/v1/bpp_intelligence_items", headers,
            params={"select": "*", "id": f"eq.{item_id}", "limit": "1"},
        )
        return _from_intelligence_row(rows[0]) if rows else None
    if source_table == SOURCE_TABLE_CLOSURE_REVIEW:
        row = comptroller_admin.get_review_by_id(item_id)
        return _from_closure_review_row(row) if row else None
    raise IntelligenceQueueError(f"Unknown source_table '{source_table}'.")


def _update_intelligence_item(item_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    supabase_url, service_role_key = get_supabase_config()
    headers = postgrest_headers(service_role_key, prefer="return=representation")
    rows = _request_json(
        "PATCH", f"{supabase_url}/rest/v1/bpp_intelligence_items", headers,
        params={"id": f"eq.{item_id}"}, json_payload=payload,
    )
    if not rows:
        raise IntelligenceQueueError(f"Intelligence item {item_id} was not found.")
    return rows[0]


def investigate_item(source_table: str, item_id: str, *, assigned_to: str | None = None) -> UnifiedIntelligenceItem:
    """Marks an item IN_REVIEW. Never touches official CAD/appraisal data --
    only this queue's own status/assignment fields."""

    if source_table == SOURCE_TABLE_INTELLIGENCE:
        row = _update_intelligence_item(item_id, {"status": "IN_REVIEW", "assigned_to": assigned_to})
        return _from_intelligence_row(row)
    if source_table == SOURCE_TABLE_CLOSURE_REVIEW:
        row = comptroller_admin.update_review_workflow(
            item_id, workflow_status="OTHER_NEEDS_RESEARCH", reviewer_notes=None, reviewed_by=assigned_to,
        )
        return _from_closure_review_row(row)
    raise IntelligenceQueueError(f"Unknown source_table '{source_table}'.")


def resolve_item(
    source_table: str,
    item_id: str,
    *,
    resolution: str,
    resolution_notes: str | None,
    reviewed_by: str | None,
) -> UnifiedIntelligenceItem:
    """Records a human decision. This is the ONLY write path for
    investigate/resolve/dismiss actions, and it only ever writes to this
    queue's own tables (workflow_status/resolution/notes/reviewer) -- never
    to property value, appraisal status, ownership, account status, BPP
    records, or exemption data. Any consequential appraisal action stays a
    human decision made through RenditionPilot's normal appraisal tools."""

    if source_table == SOURCE_TABLE_INTELLIGENCE:
        row = _update_intelligence_item(
            item_id,
            {
                "status": "RESOLVED",
                "resolution": resolution,
                "resolution_notes": resolution_notes,
                "reviewed_by": reviewed_by,
                "reviewed_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return _from_intelligence_row(row)
    if source_table == SOURCE_TABLE_CLOSURE_REVIEW:
        workflow_status = _RESOLUTION_TO_CLOSURE_WORKFLOW.get(resolution, "OTHER_NEEDS_RESEARCH")
        row = comptroller_admin.update_review_workflow(
            item_id, workflow_status=workflow_status, reviewer_notes=resolution_notes, reviewed_by=reviewed_by,
        )
        return _from_closure_review_row(row)
    raise IntelligenceQueueError(f"Unknown source_table '{source_table}'.")


def dismiss_item(
    source_table: str,
    item_id: str,
    *,
    resolution_notes: str | None,
    reviewed_by: str | None,
) -> UnifiedIntelligenceItem:
    if source_table == SOURCE_TABLE_INTELLIGENCE:
        row = _update_intelligence_item(
            item_id,
            {
                "status": "DISMISSED",
                "resolution": "FALSE_MATCH",
                "resolution_notes": resolution_notes,
                "reviewed_by": reviewed_by,
                "reviewed_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return _from_intelligence_row(row)
    if source_table == SOURCE_TABLE_CLOSURE_REVIEW:
        row = comptroller_admin.update_review_workflow(
            item_id, workflow_status="NOT_CLOSED", reviewer_notes=resolution_notes, reviewed_by=reviewed_by,
        )
        return _from_closure_review_row(row)
    raise IntelligenceQueueError(f"Unknown source_table '{source_table}'.")
