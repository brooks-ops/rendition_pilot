"""Mailing scan entry point and shared comparison types.

Production scans compare imported CRS mailing data against current Comptroller
observations. Legacy taxpayer-history flags remain review history, not CRS facts.
See mailing_comparisons.py for the current scan and durable run ledger.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.comptroller.jurisdictions import Jurisdiction, get_jurisdiction, validate_capability
from app.comptroller.mailing_address_matching import (
    FORMAT_ONLY_DIFFERENCE,
    INSUFFICIENT_DATA,
    LIKELY_CHANGE,
    POSSIBLE_CHANGE,
    SAME_ADDRESS,
    MailingAddressComparison,
    compare_mailing_addresses,
)
from app.comptroller.service import _request_json, get_supabase_config, postgrest_headers

CAPABILITY = "mailing_address_monitoring"
SIGNAL_TYPE = "mailing_address_change"
COMPTROLLER_SOURCE = "tx_comptroller_open_data"

# Spec item 10: not every source carries equal weight. A lookup table, not
# a hardcoded universal assumption -- extending it (e.g. once rendition
# extraction exists) is a one-line addition, never a code change to the
# combination logic below.
SOURCE_TRUST: dict[str, str] = {
    # Self-reported to the state on a sales-tax registration; real, but not
    # a sworn filing -- "medium" per spec item 10's own suggested weighting.
    "tx_comptroller_open_data": "MEDIUM",
    # A taxpayer's own current-year sworn rendition -- the highest-trust
    # source this system will ever see for "what is my mailing address."
    # No real data flows through this yet (see module docstring).
    "rendition_submitted_by_taxpayer": "HIGH",
}

_CONFIDENCE_ORDER = {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "NONE": 0, "UNMATCHED": 0}

RECOMMENDED_ACTIONS = {
    "LIKELY_MAILING_ADDRESS_CHANGE": "Review mailing address for possible CAD update.",
    "CONFIRMED_MAILING_ADDRESS_CHANGE": "Taxpayer-submitted address change -- review and update CAD record.",
    "POSSIBLE_MAILING_ADDRESS_CHANGE": "Possible mailing address change -- verify before updating CAD record.",
}


class MailingAddressIntelligenceError(RuntimeError):
    pass


@dataclass(frozen=True)
class MailingAddressCandidate:
    """One taxpayer's current Comptroller-reported mailing address --
    de-duplicated to one row per taxpayer_id even though a taxpayer may
    have several permit locations, since the mailing address is a
    taxpayer-level field, not a per-location one."""

    taxpayer_id: str
    legal_name: str | None
    mailing_address: str | None
    mailing_city: str | None
    mailing_state: str | None
    mailing_zip: str | None
    mailing_zip4: str | None
    retrieved_at: str | None = None
    conflicting_addresses: bool = False
    address_variants: tuple[dict[str, Any], ...] = ()

    @property
    def source_record_id(self) -> str:
        return self.taxpayer_id


@dataclass(frozen=True)
class MailingAddressIntelligenceResult:
    jurisdiction_id: str
    dry_run: bool
    evaluated: int = 0
    same_address: int = 0
    format_only: int = 0
    possible_change: int = 0
    likely_change: int = 0
    insufficient_data: int = 0
    baseline_established: int = 0
    items_created: int = 0
    items_updated: int = 0
    duplicates_suppressed: int = 0
    item_ids: list[str] = field(default_factory=list)
    observations_added: int = 0
    unlinked: int = 0
    missing_crs: int = 0
    run_id: str | None = None


def _combine_confidence(identity_confidence: str, change_confidence: str) -> str:
    """Spec item 29: identity confidence and change confidence are two
    separate questions -- a perfect address delta attached to the wrong
    account is useless, so a weak identity caps the combined result even
    when the address difference itself is unambiguous."""

    weaker = min(_CONFIDENCE_ORDER.get(identity_confidence, 0), _CONFIDENCE_ORDER.get(change_confidence, 0))
    for label, rank in _CONFIDENCE_ORDER.items():
        if rank == weaker:
            return label
    return "NONE"


def classify_mailing_address_change(
    comparison: MailingAddressComparison, *, identity_confidence: str, source: str,
) -> tuple[str | None, str | None, str]:
    """Returns (classification, priority, combined_confidence).
    classification is None when nothing should be alerted (spec:
    formatting differences must never create an alert)."""

    if comparison.classification not in (LIKELY_CHANGE, POSSIBLE_CHANGE):
        return None, None, "NONE"

    combined = _combine_confidence(identity_confidence, comparison.change_confidence)
    source_trust = SOURCE_TRUST.get(source, "MEDIUM")

    if comparison.classification == POSSIBLE_CHANGE or combined != "HIGH":
        return "POSSIBLE_MAILING_ADDRESS_CHANGE", "MEDIUM", combined
    if source_trust == "HIGH":
        return "CONFIRMED_MAILING_ADDRESS_CHANGE", "HIGH", combined
    return "LIKELY_MAILING_ADDRESS_CHANGE", "HIGH", combined


def _headers(prefer: str | None = None) -> dict[str, str]:
    _, service_role_key = get_supabase_config()
    return postgrest_headers(service_role_key, prefer=prefer)


def _base_url() -> str:
    supabase_url, _ = get_supabase_config()
    return supabase_url


def _fetch_all_permit_locations(jurisdiction: Jurisdiction, *, page_size: int = 1000) -> list[dict[str, Any]]:
    """Paginates using this module's own `_request_json` binding directly
    (rather than service._paginated_get, which would call service.py's own
    separate binding) so tests that monkeypatch
    mailing_address_intelligence._request_json still intercept it -- same
    fix, same reason, as property_adapter.py's _fetch_all()."""

    headers = _headers()
    base_params = {
        "select": "taxpayer_id,legal_name,mailing_address,mailing_city,mailing_state,mailing_zip,mailing_zip4,last_checked_at",
        "county": f"eq.{jurisdiction.county_name}",
        "current_status": "eq.ACTIVE",
        "order": "taxpayer_id.asc,id.asc",
    }
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = _request_json(
            "GET", f"{_base_url()}/rest/v1/comptroller_permit_locations", headers,
            params={**base_params, "limit": page_size, "offset": offset},
        )
        if not isinstance(page, list):
            raise MailingAddressIntelligenceError(f"Unexpected response fetching comptroller_permit_locations: {page!r}")
        if not page:
            break
        rows.extend(page)
        offset += len(page)
    return rows


def get_mailing_address_candidates(jurisdiction: Jurisdiction) -> list[MailingAddressCandidate]:
    rows = _fetch_all_permit_locations(jurisdiction)
    seen: dict[str, MailingAddressCandidate] = {}
    def variant(row):
        return {key:row.get(key) for key in ('legal_name','mailing_address','mailing_city','mailing_state','mailing_zip','mailing_zip4','last_checked_at')}
    for row in rows:
        taxpayer_id = row.get("taxpayer_id")
        if not taxpayer_id:
            continue
        if taxpayer_id in seen:
            from dataclasses import replace
            from app.comptroller.address_normalizer import normalize_mailing_address
            existing = seen[taxpayer_id]
            def key(line, city, state, zip_code):
                return normalize_mailing_address(line, city=city, state=state, zip_code=zip_code).full_normalized
            if key(existing.mailing_address, existing.mailing_city, existing.mailing_state, existing.mailing_zip) != key(
                    row.get("mailing_address"), row.get("mailing_city"), row.get("mailing_state"), row.get("mailing_zip")) or existing.mailing_zip4 != row.get("mailing_zip4"):
                seen[taxpayer_id] = replace(existing, conflicting_addresses=True, address_variants=existing.address_variants+(variant(row),))
            continue
        seen[taxpayer_id] = MailingAddressCandidate(
            taxpayer_id=taxpayer_id,
            legal_name=row.get("legal_name"),
            mailing_address=row.get("mailing_address"),
            mailing_city=row.get("mailing_city"),
            mailing_state=row.get("mailing_state"),
            mailing_zip=row.get("mailing_zip"),
            mailing_zip4=row.get("mailing_zip4"),
            retrieved_at=row.get("last_checked_at"),
            address_variants=(variant(row),),
        )
    return list(seen.values())


def get_latest_observation(
    jurisdiction: Jurisdiction, *, account_identifier_type: str, account_identifier: str,
) -> dict[str, Any] | None:
    rows = _request_json(
        "GET", f"{_base_url()}/rest/v1/mailing_address_observations", _headers(),
        params={
            "select": "*",
            "jurisdiction_id": f"eq.{jurisdiction.id}",
            "account_identifier_type": f"eq.{account_identifier_type}",
            "account_identifier": f"eq.{account_identifier}",
            "order": "observed_at.desc",
            "limit": "1",
        },
    )
    return rows[0] if isinstance(rows, list) and rows else None


def record_observation(
    jurisdiction: Jurisdiction,
    *,
    account_identifier_type: str,
    account_identifier: str,
    source: str,
    source_record_id: str | None,
    source_effective_date: str | None,
    raw_line: str | None,
    city: str | None,
    state: str | None,
    zip_code: str | None,
    dry_run: bool = False,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Append an address transition atomically; identical consecutive scans are a no-op."""
    from app.comptroller.address_normalizer import normalize_mailing_address
    normalized = normalize_mailing_address(raw_line, city=city, state=state, zip_code=zip_code)
    if not normalized.normalized_line or dry_run:
        return {"added": False, "observation": None}
    return _request_json(
        "POST", f"{_base_url()}/rest/v1/rpc/mailing_record_observation", _headers(),
        json_payload={"p_observation": {
            "jurisdiction_id": jurisdiction.id, "account_identifier_type": account_identifier_type, "scan_run_id": run_id,
            "account_identifier": account_identifier, "source": source, "source_record_id": source_record_id,
            "source_effective_date": source_effective_date, "raw_address_line": raw_line,
            "address_type": normalized.address_type, "po_box_number": normalized.po_box_number,
            "unit": normalized.unit, "city": normalized.city, "state": normalized.state,
            "zip": normalized.zip5, "zip4": normalized.zip4,
            "normalized_full_address": normalized.full_normalized + ("-"+normalized.zip4 if normalized.zip4 else ""),
        }},
    )


def run_mailing_address_intelligence(jurisdiction_id: str, *, dry_run: bool = False) -> MailingAddressIntelligenceResult:
    from app.comptroller.mailing_comparisons import run_scan
    return run_scan(jurisdiction_id, dry_run=dry_run)
