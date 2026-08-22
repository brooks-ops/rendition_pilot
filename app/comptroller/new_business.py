"""New Business Detection: identify newly-active Comptroller sales-tax
locations in a jurisdiction that don't appear to have an existing
RenditionPilot BPP account.

Reuses the existing Comptroller ingestion pipeline entirely -- there is no
second integration here. Detection queries `comptroller_permit_locations`
(already kept current by the daily sales-tax sync in
app.comptroller.service) rather than fetching Comptroller data again, and
reuses app.comptroller.matching's scorer via the jurisdiction's CadAdapter
(app.comptroller.cad_adapter) rather than a new matcher.

DEFINING "NEW BUSINESS" WITHOUT AN EXPLICIT "OPENED DATE":
The Comptroller dataset has no reliable, explicit "date this business
opened" field. `permit_date` (stored as `permit_start_date` on
comptroller_permit_locations) is when the Comptroller issued the sales-tax
permit -- usually close to when the business started operating, but not
guaranteed to be, and sometimes historical/backfilled. Rather than trust a
single vendor-supplied date, this module reuses the same
snapshot-comparison principle the sales-tax closure monitor already
established: a permit is a "new business candidate" when RenditionPilot's
OWN ingestion first observed it (`first_seen_at`) during a non-baseline sync
(`is_baseline = false`) and it is currently ACTIVE. That is a fact
RenditionPilot itself recorded by comparing successive daily snapshots, not
a date invented or blindly trusted from the source. `permit_start_date` is
preserved on every intelligence item as corroborating evidence, never used
as the sole detection signal.

LIMITATION (documented, not silently ignored): a permit that quietly existed
for years but only recently came into scope for some reason (a data
correction, a county boundary reassignment) would be indistinguishable from
a genuine new business under this method. Lubbock County has been fully
baselined, making this unlikely in practice, but it is a real limitation of
using "first observed by us" as the signal, inherent to the source data not
providing a trustworthy opened-date.

Idempotent, one-time-per-permit evaluation: `new_business_evaluated_at` on
comptroller_permit_locations marks a permit as already assessed (set
regardless of outcome, even when no intelligence item was warranted) so
repeated runs don't re-fetch/re-match the same permit forever. Unlike
sales-tax status (re-checked every sync because it can change), "was this a
new business" is a one-time determination for V1 -- ownership
change/relocation over time are separate, future signal types (see
docs/bpp_intelligence_queue.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.comptroller.cad_adapter import get_cad_adapter
from app.comptroller.jurisdictions import Jurisdiction, get_jurisdiction, validate_capability
from app.comptroller.matching import MatchCandidate, MatchResult, match_closure_to_account
from app.comptroller.property_adapter import PropertySourceError, get_property_adapter
from app.comptroller.property_enrichment import CAPABILITY as PROPERTY_CAPABILITY
from app.comptroller.property_enrichment import PropertyEnrichmentError, run_property_enrichment
from app.comptroller.service import _paginated_get, _request_json, get_supabase_config, postgrest_headers

CAPABILITY = "new_business_detection"
SIGNAL_TYPE = "new_business"
SOURCE = "tx_comptroller_open_data"

RECOMMENDED_ACTIONS = {
    "NO_ACCOUNT_FOUND": "Review for possible new BPP account.",
    "POSSIBLE_EXISTING_ACCOUNT": "Verify whether this location already has a BPP account before creating a new one.",
    "AMBIGUOUS": "Multiple RenditionPilot records scored similarly for this business; investigate before deciding.",
}


class NewBusinessDetectionError(RuntimeError):
    pass


@dataclass(frozen=True)
class NewBusinessCandidate:
    permit_location_id: str
    taxpayer_id: str
    location_number: str
    legal_name: str | None
    location_name: str | None
    address: str | None
    city: str | None
    state: str | None
    zip: str | None
    permit_start_date: str | None
    current_status: str
    first_seen_at: str
    source_dataset_id: str | None

    @property
    def source_record_id(self) -> str:
        return f"{self.taxpayer_id}:{self.location_number}"


@dataclass(frozen=True)
class NewBusinessDetectionResult:
    jurisdiction_id: str
    dry_run: bool
    evaluated: int = 0
    existing_high_confidence: int = 0
    possible_existing: int = 0
    no_account_found: int = 0
    ambiguous: int = 0
    items_created: int = 0
    items_updated: int = 0
    duplicates_suppressed: int = 0
    item_ids: list[str] = field(default_factory=list)


def get_new_business_candidates(jurisdiction: Jurisdiction, *, reevaluate: bool = False) -> list[NewBusinessCandidate]:
    """Permit locations RenditionPilot itself first observed after baseline,
    currently ACTIVE. Excludes already-evaluated permits unless `reevaluate`
    is set (for a manual re-run, e.g. after fixing a matching bug).

    Filters on `county` (text), not `jurisdiction_id`: comptroller_permit_locations
    is written by app.comptroller.service.sync_county(), which is the
    already-deployed, already-running sales-tax closure monitor's write path
    and is deliberately not modified by this feature -- it has never
    populated jurisdiction_id and isn't being changed to. `county` has
    reliably held the jurisdiction's county name since that table's creation
    and needs no backfill dependency to be correct today. jurisdiction_id is
    still backfilled (see the migration) and stored on every
    bpp_intelligence_items row for direct FK-based queries going forward.
    """

    supabase_url, service_role_key = get_supabase_config()
    headers = postgrest_headers(service_role_key)
    params: dict[str, Any] = {
        "select": (
            "id,taxpayer_id,location_number,legal_name,location_name,address,city,state,zip,"
            "permit_start_date,current_status,first_seen_at,source_dataset_id"
        ),
        "county": f"eq.{jurisdiction.county_name}",
        "is_baseline": "eq.false",
        "current_status": "eq.ACTIVE",
    }
    if not reevaluate:
        params["new_business_evaluated_at"] = "is.null"
    rows = _paginated_get(supabase_url, headers, "comptroller_permit_locations", params, page_size=500)
    return [
        NewBusinessCandidate(
            permit_location_id=row["id"],
            taxpayer_id=row["taxpayer_id"],
            location_number=row["location_number"],
            legal_name=row.get("legal_name"),
            location_name=row.get("location_name"),
            address=row.get("address"),
            city=row.get("city"),
            state=row.get("state"),
            zip=row.get("zip"),
            permit_start_date=row.get("permit_start_date"),
            current_status=row["current_status"],
            first_seen_at=row["first_seen_at"],
            source_dataset_id=row.get("source_dataset_id"),
        )
        for row in rows
    ]


def classify_match(match_result: MatchResult) -> tuple[str, str]:
    """Returns (classification, priority). Ambiguity overrides confidence
    tier -- "multiple plausible candidates" is its own bucket regardless of
    how strong any single one of them scored (spec: "Multiple accounts or
    conflicting evidence prevent a reliable determination")."""

    if match_result.ambiguous:
        return "AMBIGUOUS", "MEDIUM"
    if match_result.confidence == "HIGH":
        return "EXISTING_ACCOUNT_HIGH_CONFIDENCE", "LOW"
    if match_result.confidence in ("MEDIUM", "LOW"):
        return "POSSIBLE_EXISTING_ACCOUNT", "MEDIUM"
    return "NO_ACCOUNT_FOUND", "HIGH"


def _find_existing_item(source_record_id: str) -> dict[str, Any] | None:
    supabase_url, service_role_key = get_supabase_config()
    headers = postgrest_headers(service_role_key)
    rows = _request_json(
        "GET",
        f"{supabase_url}/rest/v1/bpp_intelligence_items",
        headers,
        params={
            "select": "id,status",
            "signal_type": f"eq.{SIGNAL_TYPE}",
            "source": f"eq.{SOURCE}",
            "source_record_id": f"eq.{source_record_id}",
            "limit": "1",
        },
    )
    return rows[0] if rows else None


def _create_item(payload: dict[str, Any]) -> dict[str, Any]:
    supabase_url, service_role_key = get_supabase_config()
    headers = postgrest_headers(service_role_key, prefer="return=representation")
    rows = _request_json("POST", f"{supabase_url}/rest/v1/bpp_intelligence_items", headers, json_payload=payload)
    return rows[0] if isinstance(rows, list) else rows


def _update_item_evidence(item_id: str, payload: dict[str, Any]) -> None:
    supabase_url, service_role_key = get_supabase_config()
    headers = postgrest_headers(service_role_key)
    _request_json(
        "PATCH",
        f"{supabase_url}/rest/v1/bpp_intelligence_items",
        headers,
        params={"id": f"eq.{item_id}"},
        json_payload=payload,
    )


def _mark_evaluated(permit_location_id: str) -> None:
    supabase_url, service_role_key = get_supabase_config()
    headers = postgrest_headers(service_role_key)
    _request_json(
        "PATCH",
        f"{supabase_url}/rest/v1/comptroller_permit_locations",
        headers,
        params={"id": f"eq.{permit_location_id}"},
        json_payload={"new_business_evaluated_at": datetime.now(timezone.utc).isoformat()},
    )


def _build_item_payload(
    jurisdiction: Jurisdiction,
    candidate: NewBusinessCandidate,
    match_result: MatchResult,
    classification: str,
    priority: str,
    property_match: Any | None = None,
) -> dict[str, Any]:
    matched = match_result.candidate
    matched_property = getattr(property_match, "matched_property", None) if property_match is not None else None
    return {
        "jurisdiction_id": jurisdiction.id,
        "district_id": jurisdiction.district_id,
        "signal_type": SIGNAL_TYPE,
        "source": SOURCE,
        "source_dataset_id": candidate.source_dataset_id,
        "source_record_id": candidate.source_record_id,
        "source_taxpayer_id": candidate.taxpayer_id,
        "source_location_number": candidate.location_number,
        "source_permit_location_id": candidate.permit_location_id,
        "classification": classification,
        "priority": priority,
        "confidence": match_result.confidence,
        "confidence_score": match_result.score,
        "is_ambiguous": match_result.ambiguous,
        "business_name": candidate.location_name,
        "legal_name": candidate.legal_name,
        "source_address": candidate.address,
        "source_city": candidate.city,
        "source_state": candidate.state,
        "source_zip": candidate.zip,
        "permit_start_date": candidate.permit_start_date,
        "current_status": candidate.current_status,
        "first_detected_at": candidate.first_seen_at,
        "matched_record_id": matched.record_id if matched else None,
        "matched_account_number": matched.account_number if matched else None,
        "matched_owner_name": matched.owner_name if matched else None,
        "matched_address": matched_property.situs_address_raw if matched_property else None,
        "property_account_number": matched_property.real_account_number if matched_property else None,
        "property_match_status": getattr(property_match, "classification", None),
        "property_record_id": matched_property.property_id if matched_property else None,
        "tug": matched_property.tug if matched_property else None,
        "neighborhood": matched_property.neighborhood if matched_property else None,
        "map_id": matched_property.map_id if matched_property else None,
        "match_score": match_result.score,
        "match_reason": match_result.reason,
        "match_signals": match_result.signals,
        "recommended_action": RECOMMENDED_ACTIONS.get(classification),
        "evidence": {
            "first_seen_at": candidate.first_seen_at,
            "permit_start_date": candidate.permit_start_date,
            "detection_basis": (
                "First observed by RenditionPilot's Comptroller ingestion after the initial county baseline, "
                "and currently ACTIVE -- see app/comptroller/new_business.py module docstring for why this "
                "(rather than a Comptroller-supplied 'opened date', which doesn't reliably exist) is the "
                "detection signal used."
            ),
            "name_signals_diverge": match_result.name_signals_diverge,
        },
        "status": "NEW",
    }


def run_new_business_detection(
    jurisdiction_id: str,
    *,
    dry_run: bool = False,
    reevaluate: bool = False,
) -> NewBusinessDetectionResult:
    jurisdiction = get_jurisdiction(jurisdiction_id)
    adapter = get_cad_adapter(jurisdiction)

    validation = validate_capability(jurisdiction, CAPABILITY, adapter.AVAILABLE_ACCOUNT_FIELDS)
    if not validation.ok:
        raise NewBusinessDetectionError(validation.message)

    # Property Enrichment is optional, additive corroboration -- a
    # jurisdiction with no property data loaded (the default; see
    # property_enrichment.py) must never block or change New Business
    # Detection's existing name-only behavior. Any failure here silently
    # disables enrichment for this run rather than raising.
    all_properties: list | None = None
    if jurisdiction.has_capability(PROPERTY_CAPABILITY):
        property_validation = validate_capability(
            jurisdiction, PROPERTY_CAPABILITY, frozenset(jurisdiction.property_field_mapping.keys())
        )
        if property_validation.ok:
            try:
                property_adapter_instance = get_property_adapter(jurisdiction)
                all_properties = property_adapter_instance.search_properties(jurisdiction)
            except PropertySourceError:
                all_properties = None

    candidates = get_new_business_candidates(jurisdiction, reevaluate=reevaluate)

    # Fetch once, reuse for every candidate in this run (mirrors month_end.py's
    # per-district account caching -- one fetch, not one per candidate).
    normalized_accounts = adapter.get_bpp_accounts(jurisdiction)
    match_candidates = [
        MatchCandidate(
            record_id=account.account_id,
            account_number=account.account_number,
            owner_name=account.owner_name,
            tax_year=account.tax_year,
        )
        for account in normalized_accounts
    ]

    counts = {
        "existing_high_confidence": 0,
        "possible_existing": 0,
        "no_account_found": 0,
        "ambiguous": 0,
    }
    items_created = 0
    items_updated = 0
    duplicates_suppressed = 0
    item_ids: list[str] = []

    for candidate in candidates:
        property_match = None
        if all_properties is not None and candidate.address:
            try:
                outcome = run_property_enrichment(
                    jurisdiction,
                    subject_type="NEW_BUSINESS_CANDIDATE",
                    subject_id=candidate.source_record_id,
                    input_address=candidate.address,
                    input_zip=candidate.zip,
                    candidates=all_properties,
                    dry_run=dry_run,
                )
                property_match = outcome.result
            except PropertyEnrichmentError:
                property_match = None

        match_result = match_closure_to_account(
            district_id=jurisdiction.district_id,
            permit_legal_name=candidate.legal_name,
            permit_location_name=candidate.location_name,
            candidates=match_candidates,
            property_match=property_match,
        )
        classification, priority = classify_match(match_result)

        if classification == "AMBIGUOUS":
            counts["ambiguous"] += 1
        elif classification == "EXISTING_ACCOUNT_HIGH_CONFIDENCE":
            counts["existing_high_confidence"] += 1
        elif classification == "POSSIBLE_EXISTING_ACCOUNT":
            counts["possible_existing"] += 1
        else:
            counts["no_account_found"] += 1

        # "Do not create an intelligence alert by default" for a
        # high-confidence existing-account match with no other discrepancy --
        # there is currently no second signal (e.g. address-based relocation
        # detection) available to detect "another meaningful discrepancy"
        # with, so this classification never creates an item. Documented as
        # a limitation, not silently dropped.
        create_alert = classification != "EXISTING_ACCOUNT_HIGH_CONFIDENCE"

        if not dry_run:
            if create_alert:
                existing = _find_existing_item(candidate.source_record_id)
                payload = _build_item_payload(jurisdiction, candidate, match_result, classification, priority, property_match)
                if existing is None:
                    created = _create_item(payload)
                    items_created += 1
                    item_ids.append(created["id"])
                elif existing["status"] in ("NEW", "IN_REVIEW"):
                    # Preserve status/assigned_to/resolution -- only refresh
                    # the evidence/match fields so staff see current data.
                    update_payload = {k: v for k, v in payload.items() if k != "status"}
                    _update_item_evidence(existing["id"], update_payload)
                    items_updated += 1
                    item_ids.append(existing["id"])
                else:
                    # RESOLVED/DISMISSED: preserve historical intelligence --
                    # never recreate or reopen a decision staff already made.
                    duplicates_suppressed += 1
            _mark_evaluated(candidate.permit_location_id)

    return NewBusinessDetectionResult(
        jurisdiction_id=jurisdiction.id,
        dry_run=dry_run,
        evaluated=len(candidates),
        existing_high_confidence=counts["existing_high_confidence"],
        possible_existing=counts["possible_existing"],
        no_account_found=counts["no_account_found"],
        ambiguous=counts["ambiguous"],
        items_created=items_created,
        items_updated=items_updated,
        duplicates_suppressed=duplicates_suppressed,
        item_ids=item_ids,
    )
