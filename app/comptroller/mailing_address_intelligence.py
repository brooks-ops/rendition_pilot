"""Mailing Address Intelligence: detects and evidences probable mailing-
address changes for BPP taxpayers, and writes review-ready items into the
existing BPP Intelligence Queue (signal_type='mailing_address_change').

WHAT RUNS AUTOMATICALLY TODAY: the only real, verified mailing-address
source is the Texas Comptroller feed's taxpayer address (`tp_address` et
al -- see client.py's PermitRecord docstring), compared against
RenditionPilot's OWN previously observed value for that same taxpayer_id
(never a source-supplied "this changed" flag -- the Comptroller feed has
none). Identity is trivial and exact for this path: the same `taxpayer_id`
IS the same taxpayer, no fuzzy name-matching needed.

WHAT THIS MODULE IS ALSO BUILT FOR, BUT DOES NOT YET RUN AUTOMATICALLY:
comparing a rendition's own mailing address against a BPP account's prior
observations (spec item 11's "next rendition season" flow). The rendition
OCR pipeline (app/pipeline.py::_extract_metadata) does not currently
extract a mailing address at all -- extending core OCR extraction without
real rendition PDFs to validate against was deliberately left out of this
pass rather than shipped unverified. `record_observation()`/
`compare_against_latest_observation()` below are already identity-type-
generic (`account_identifier_type="bpp_account"` is a first-class option)
specifically so wiring this up later is a config/call-site change, not a
redesign, once that extraction exists.

Reuses, never forks: address_normalizer.py's normalize_mailing_address(),
mailing_address_matching.py's compare_mailing_addresses(), the existing
bpp_intelligence_items dedup/lifecycle machinery (intelligence.py), and the
same jurisdiction/capability pattern every other module here follows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
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
        "select": "taxpayer_id,legal_name,mailing_address,mailing_city,mailing_state,mailing_zip,mailing_zip4",
        "county": f"eq.{jurisdiction.county_name}",
        "current_status": "eq.ACTIVE",
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
    for row in rows:
        taxpayer_id = row.get("taxpayer_id")
        if not taxpayer_id or taxpayer_id in seen:
            continue
        seen[taxpayer_id] = MailingAddressCandidate(
            taxpayer_id=taxpayer_id,
            legal_name=row.get("legal_name"),
            mailing_address=row.get("mailing_address"),
            mailing_city=row.get("mailing_city"),
            mailing_state=row.get("mailing_state"),
            mailing_zip=row.get("mailing_zip"),
            mailing_zip4=row.get("mailing_zip4"),
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
) -> None:
    """Appends one observation to history, IF it's actually a distinct
    address for this identity+source -- the dedup unique index makes
    re-observing the same address on a later run a no-op via
    resolution=ignore-duplicates, so a daily scan of thousands of unchanged
    taxpayers never floods this table (spec item 21: history, not a daily
    snapshot log)."""

    from app.comptroller.address_normalizer import normalize_mailing_address

    normalized = normalize_mailing_address(raw_line, city=city, state=state, zip_code=zip_code)
    if not normalized.full_normalized:
        return
    if dry_run:
        return
    _request_json(
        "POST", f"{_base_url()}/rest/v1/mailing_address_observations",
        _headers(prefer="resolution=ignore-duplicates,return=minimal"),
        params={"on_conflict": "jurisdiction_id,account_identifier_type,account_identifier,source,normalized_full_address"},
        json_payload=[{
            "jurisdiction_id": jurisdiction.id,
            "account_identifier_type": account_identifier_type,
            "account_identifier": account_identifier,
            "source": source,
            "source_record_id": source_record_id,
            "source_effective_date": source_effective_date,
            "raw_address_line": raw_line,
            "address_type": normalized.address_type,
            "po_box_number": normalized.po_box_number,
            "unit": normalized.unit,
            "city": normalized.city,
            "state": normalized.state,
            "zip": normalized.zip5,
            "zip4": normalized.zip4,
            "normalized_full_address": normalized.full_normalized,
        }],
    )


def _find_existing_item(source_record_id: str) -> dict[str, Any] | None:
    rows = _request_json(
        "GET", f"{_base_url()}/rest/v1/bpp_intelligence_items", _headers(),
        params={
            "select": "id,status", "signal_type": f"eq.{SIGNAL_TYPE}",
            "source": f"eq.{COMPTROLLER_SOURCE}", "source_record_id": f"eq.{source_record_id}", "limit": "1",
        },
    )
    return rows[0] if rows else None


def _create_item(payload: dict[str, Any]) -> dict[str, Any]:
    rows = _request_json(
        "POST", f"{_base_url()}/rest/v1/bpp_intelligence_items", _headers(prefer="return=representation"),
        json_payload=payload,
    )
    return rows[0] if isinstance(rows, list) else rows


def _update_item_evidence(item_id: str, payload: dict[str, Any]) -> None:
    _request_json(
        "PATCH", f"{_base_url()}/rest/v1/bpp_intelligence_items", _headers(),
        params={"id": f"eq.{item_id}"}, json_payload=payload,
    )


def _build_item_payload(
    jurisdiction: Jurisdiction, candidate: MailingAddressCandidate, comparison: MailingAddressComparison,
    classification: str, priority: str, combined_confidence: str,
) -> dict[str, Any]:
    current = comparison.current.full_normalized if comparison.current else None
    observed = comparison.observed.full_normalized if comparison.observed else None
    # A change to the SAME identity can happen more than once over time
    # (spec item 20) -- the observed address is part of the dedup key so a
    # later, different change is never suppressed by an earlier one.
    source_record_id = f"{candidate.taxpayer_id}:{observed}"
    return {
        "jurisdiction_id": jurisdiction.id,
        "district_id": jurisdiction.district_id,
        "signal_type": SIGNAL_TYPE,
        "source": COMPTROLLER_SOURCE,
        "source_record_id": source_record_id,
        "source_taxpayer_id": candidate.taxpayer_id,
        "status": "NEW",
        "classification": classification,
        "priority": priority,
        "confidence": combined_confidence,
        "confidence_score": None,
        "is_ambiguous": False,
        "business_name": candidate.legal_name,
        "legal_name": candidate.legal_name,
        "mailing_address_current": current,
        "mailing_address_observed": observed,
        "first_detected_at": datetime.now(timezone.utc).isoformat(),
        "match_reason": "; ".join(comparison.reasons),
        "match_signals": comparison.differences,
        "recommended_action": RECOMMENDED_ACTIONS.get(classification),
        "evidence": {
            "source_effective_date": None,
            "detection_basis": (
                "Comptroller-reported taxpayer mailing address differs from RenditionPilot's own "
                "previously observed value for this taxpayer -- see app/comptroller/mailing_address_intelligence.py."
            ),
        },
    }


def run_mailing_address_intelligence(
    jurisdiction_id: str, *, dry_run: bool = False,
) -> MailingAddressIntelligenceResult:
    jurisdiction = get_jurisdiction(jurisdiction_id)

    validation = validate_capability(jurisdiction, CAPABILITY, frozenset({"mailing_address"}))
    if not validation.ok:
        raise MailingAddressIntelligenceError(validation.message)

    candidates = get_mailing_address_candidates(jurisdiction)

    counts = {
        "same_address": 0, "format_only": 0, "possible_change": 0,
        "likely_change": 0, "insufficient_data": 0, "baseline_established": 0,
    }
    items_created = 0
    items_updated = 0
    duplicates_suppressed = 0
    item_ids: list[str] = []

    from app.comptroller.address_normalizer import normalize_mailing_address

    for candidate in candidates:
        normalized_candidate = normalize_mailing_address(
            candidate.mailing_address, city=candidate.mailing_city,
            state=candidate.mailing_state, zip_code=candidate.mailing_zip,
        )
        if not normalized_candidate.full_normalized:
            # Genuinely nothing to record or compare -- not a baseline, not
            # a comparison, just unusable data for this candidate.
            counts["insufficient_data"] += 1
            continue

        prior = get_latest_observation(
            jurisdiction, account_identifier_type="comptroller_taxpayer", account_identifier=candidate.taxpayer_id,
        )

        if prior is None:
            counts["baseline_established"] += 1
            record_observation(
                jurisdiction, account_identifier_type="comptroller_taxpayer", account_identifier=candidate.taxpayer_id,
                source=COMPTROLLER_SOURCE, source_record_id=candidate.source_record_id, source_effective_date=None,
                raw_line=candidate.mailing_address, city=candidate.mailing_city, state=candidate.mailing_state,
                zip_code=candidate.mailing_zip, dry_run=dry_run,
            )
            continue

        comparison = compare_mailing_addresses(
            current_raw=prior.get("raw_address_line"), current_city=prior.get("city"),
            current_state=prior.get("state"), current_zip=prior.get("zip"),
            observed_raw=candidate.mailing_address, observed_city=candidate.mailing_city,
            observed_state=candidate.mailing_state, observed_zip=candidate.mailing_zip,
        )

        if comparison.classification == SAME_ADDRESS:
            counts["same_address"] += 1
        elif comparison.classification == FORMAT_ONLY_DIFFERENCE:
            counts["format_only"] += 1
        elif comparison.classification == POSSIBLE_CHANGE:
            counts["possible_change"] += 1
        elif comparison.classification == LIKELY_CHANGE:
            counts["likely_change"] += 1
        else:
            counts["insufficient_data"] += 1

        # Same taxpayer_id IS the same taxpayer -- identity is exact for
        # this comparison, no fuzzy matching involved (unlike the future
        # rendition-vs-BPP-account path, which would reuse matching.py's
        # real identity-confidence logic instead of this constant).
        classification, priority, combined_confidence = classify_mailing_address_change(
            comparison, identity_confidence="HIGH", source=COMPTROLLER_SOURCE,
        )

        if classification and not dry_run:
            payload = _build_item_payload(jurisdiction, candidate, comparison, classification, priority, combined_confidence)
            existing = _find_existing_item(payload["source_record_id"])
            if existing is None:
                created = _create_item(payload)
                items_created += 1
                item_ids.append(created["id"])
            elif existing["status"] in ("NEW", "IN_REVIEW"):
                update_payload = {k: v for k, v in payload.items() if k != "status"}
                _update_item_evidence(existing["id"], update_payload)
                items_updated += 1
                item_ids.append(existing["id"])
            else:
                duplicates_suppressed += 1

        # Record the new observation regardless of classification (even
        # SAME_ADDRESS/FORMAT_ONLY) so history's dedup index sees today's
        # exact normalized form -- harmless no-op via ignore-duplicates when
        # it matches what's already on file.
        record_observation(
            jurisdiction, account_identifier_type="comptroller_taxpayer", account_identifier=candidate.taxpayer_id,
            source=COMPTROLLER_SOURCE, source_record_id=candidate.source_record_id, source_effective_date=None,
            raw_line=candidate.mailing_address, city=candidate.mailing_city, state=candidate.mailing_state,
            zip_code=candidate.mailing_zip, dry_run=dry_run,
        )

    return MailingAddressIntelligenceResult(
        jurisdiction_id=jurisdiction.id, dry_run=dry_run, evaluated=len(candidates),
        same_address=counts["same_address"], format_only=counts["format_only"],
        possible_change=counts["possible_change"], likely_change=counts["likely_change"],
        insufficient_data=counts["insufficient_data"], baseline_established=counts["baseline_established"],
        items_created=items_created, items_updated=items_updated, duplicates_suppressed=duplicates_suppressed,
        item_ids=item_ids,
    )
