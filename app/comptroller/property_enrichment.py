"""Property Enrichment orchestration: caching, batching, and the
"which BPP accounts already sit at this property" cross-reference.

Writes only to `property_enrichment_results` (advisory/review data, one
cached row per subject) -- never to `real_property_records` (that's
property_import.py's job) and never to any official CAD data. See
docs/property_enrichment.md for the full design and the safety guarantee.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.comptroller.jurisdictions import Jurisdiction, validate_capability
from app.comptroller.property_adapter import ImportedPropertyAdapter, NormalizedRealProperty, get_property_adapter
from app.comptroller.property_matching import PropertyMatchResult, match_property, normalize_account_number
from app.comptroller.service import ComptrollerServiceError, _request_json, get_supabase_config, postgrest_headers

CAPABILITY = "real_property_linkage"

_RESULT_SELECT = (
    "id,jurisdiction_id,subject_type,subject_id,input_address,normalized_input_address,"
    "property_record_id,real_account_number,match_status,confidence,confidence_score,"
    "candidate_count,match_reason,signals,tug,neighborhood,map_id,source_import_id,"
    "review_status,created_at,updated_at"
)


class PropertyEnrichmentError(RuntimeError):
    pass


@dataclass(frozen=True)
class PropertyEnrichmentOutcome:
    result: PropertyMatchResult
    from_cache: bool
    stored_row_id: str | None


def _headers() -> dict[str, str]:
    try:
        _, service_role_key = get_supabase_config()
    except ComptrollerServiceError as exc:
        raise PropertyEnrichmentError(str(exc)) from exc
    return postgrest_headers(service_role_key, prefer="return=representation")


def _base_url() -> str:
    supabase_url, _ = get_supabase_config()
    return supabase_url


def _latest_import_id(jurisdiction_id: str) -> str | None:
    rows = _request_json(
        "GET",
        f"{_base_url()}/rest/v1/property_source_imports",
        _headers(),
        params={
            "select": "id",
            "jurisdiction_id": f"eq.{jurisdiction_id}",
            "order": "imported_at.desc",
            "limit": "1",
        },
    )
    if isinstance(rows, list) and rows:
        return rows[0]["id"]
    return None


def _find_cached_result(jurisdiction_id: str, subject_type: str, subject_id: str) -> dict | None:
    rows = _request_json(
        "GET",
        f"{_base_url()}/rest/v1/property_enrichment_results",
        _headers(),
        params={
            "select": _RESULT_SELECT,
            "jurisdiction_id": f"eq.{jurisdiction_id}",
            "subject_type": f"eq.{subject_type}",
            "subject_id": f"eq.{subject_id}",
            "limit": "1",
        },
    )
    if isinstance(rows, list) and rows:
        return rows[0]
    return None


def _upsert_result(payload: dict) -> dict:
    supabase_url, service_role_key = get_supabase_config()
    headers = postgrest_headers(service_role_key, prefer="resolution=merge-duplicates,return=representation")
    rows = _request_json(
        "POST",
        f"{supabase_url}/rest/v1/property_enrichment_results",
        headers,
        params={"on_conflict": "jurisdiction_id,subject_type,subject_id"},
        # review_status is deliberately omitted: the column default
        # (NOT_REVIEWED) applies on first insert, and a refresh of an
        # existing row must never silently overwrite a reviewer's prior
        # ACCEPTED/REJECTED decision.
        json_payload=[payload],
    )
    if isinstance(rows, list) and rows:
        return rows[0]
    raise PropertyEnrichmentError(f"Unexpected response upserting property_enrichment_results: {rows!r}")


def _result_to_payload(
    jurisdiction_id: str,
    subject_type: str,
    subject_id: str,
    input_address: str | None,
    match: PropertyMatchResult,
    source_import_id: str | None,
) -> dict:
    matched = match.matched_property
    return {
        "jurisdiction_id": jurisdiction_id,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "input_address": input_address,
        "normalized_input_address": match.normalized_input.normalized if match.normalized_input else None,
        "property_record_id": matched.property_id if matched else None,
        "real_account_number": matched.real_account_number if matched else None,
        "match_status": match.classification,
        "confidence": match.confidence,
        "confidence_score": match.score,
        "candidate_count": match.candidate_count,
        "match_reason": "; ".join(match.reasons),
        "signals": match.signals,
        "tug": matched.tug if matched else None,
        "neighborhood": matched.neighborhood if matched else None,
        "map_id": matched.map_id if matched else None,
        "source_import_id": source_import_id,
    }


def run_property_enrichment(
    jurisdiction: Jurisdiction,
    *,
    subject_type: str,
    subject_id: str,
    input_address: str | None,
    input_zip: str | None = None,
    force_refresh: bool = False,
    dry_run: bool = False,
    adapter: ImportedPropertyAdapter | None = None,
    candidates: list[NormalizedRealProperty] | None = None,
) -> PropertyEnrichmentOutcome:
    """Enrich one subject's address against `jurisdiction`'s real-property
    records, reusing a cached result when the input and source data haven't
    changed (spec item 22), and refreshing when either has (spec item 23).
    """

    adapter = adapter or get_property_adapter(jurisdiction)
    validation = validate_capability(jurisdiction, CAPABILITY, frozenset(jurisdiction.property_field_mapping.keys()))
    if not validation.ok:
        raise PropertyEnrichmentError(validation.message)

    latest_import_id = _latest_import_id(jurisdiction.id)
    cached = None if force_refresh else _find_cached_result(jurisdiction.id, subject_type, subject_id)

    if cached is not None:
        from app.comptroller.address_normalizer import normalize_address

        normalized_now = normalize_address(input_address, zip_code=input_zip).normalized
        cache_fresh = (
            cached.get("normalized_input_address") == normalized_now
            and cached.get("source_import_id") == latest_import_id
        )
        if cache_fresh:
            match = PropertyMatchResult(
                classification=cached["match_status"],
                confidence=cached["confidence"],
                score=float(cached.get("confidence_score") or 0.0),
                matched_property=None,
                candidate_count=cached.get("candidate_count") or 0,
                reasons=[cached.get("match_reason") or ""],
                signals=cached.get("signals") or {},
            )
            if cached.get("property_record_id"):
                match = _rehydrate_matched_property(adapter, jurisdiction, cached, match)
            return PropertyEnrichmentOutcome(result=match, from_cache=True, stored_row_id=cached["id"])

    if candidates is None:
        from app.comptroller.address_normalizer import normalize_address

        base = normalize_address(input_address, zip_code=input_zip).base_address
        candidates = adapter.find_properties_by_address(jurisdiction, base) if base else []

    match = match_property(input_address, input_zip=input_zip, candidates=candidates)

    if dry_run:
        return PropertyEnrichmentOutcome(result=match, from_cache=False, stored_row_id=None)

    payload = _result_to_payload(jurisdiction.id, subject_type, subject_id, input_address, match, latest_import_id)
    stored = _upsert_result(payload)
    return PropertyEnrichmentOutcome(result=match, from_cache=False, stored_row_id=stored.get("id"))


def _rehydrate_matched_property(
    adapter: ImportedPropertyAdapter, jurisdiction: Jurisdiction, cached: dict, match: PropertyMatchResult
) -> PropertyMatchResult:
    matched = adapter.get_property_by_id(jurisdiction, cached["property_record_id"])
    if matched is None:
        return match
    return PropertyMatchResult(
        classification=match.classification,
        confidence=match.confidence,
        score=match.score,
        matched_property=matched,
        candidate_count=match.candidate_count,
        reasons=match.reasons,
        signals=match.signals,
    )


def same_property_accounts(
    property_match: PropertyMatchResult, match_candidates: list
) -> list:
    """Which of a jurisdiction's existing BPP/rendition records already carry
    the SAME real-property account number as a matched property -- spec item
    15. Cross-references property_match's real_account_number against
    MatchCandidate.account_number (the number printed on the rendition
    itself); does not assume the property record's own owner_name field
    means anything about RenditionPilot account ownership."""

    if property_match.matched_property is None or not property_match.matched_property.real_account_number:
        return []
    target = normalize_account_number(property_match.matched_property.real_account_number)
    if not target:
        return []
    return [c for c in match_candidates if normalize_account_number(getattr(c, "account_number", None)) == target]
