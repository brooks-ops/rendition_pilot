"""New Account Enrichment: the final leg of the pipeline Property Enrichment
was built to support (see docs/property_enrichment.md):

    New BPP Account -> Address normalization -> Property Enrichment ->
    PropertyID -> R account -> TUG -> Neighborhood -> Map ->
    Appraiser assignment -> Suggested property link -> Account card

Everything through "Suggested property link" already exists -- New Business
Detection already runs Property Enrichment for every candidate and stores
the result on the intelligence item. This module adds the two genuinely new
pieces: appraiser assignment (a jurisdiction-configurable TUG/neighborhood ->
appraiser mapping) and the account card itself, which bundles everything
already known into one staff-facing summary.

An "account card" is a REPORT, not a new database entity: everything in it
is reconstructible on demand from the intelligence item, real_property_records,
and appraiser_assignment_rules, so there is nothing here to go stale or need
its own audit trail beyond `account_card_generated_at` on the source item.

SAFETY: this module only ever reads. It does not create a BPP account, does
not write to real_property_records, and does not touch any official CAD
data. The account card is advisory output for a human to use when manually
creating an account in the CAD's actual system -- exactly like every other
"Suggested Property Link" in this codebase.

Only applies to `new_business` signal-type intelligence items -- "new
account needed" is a new-business resolution outcome (see
RESOLUTION_OPTIONS_BY_SIGNAL_TYPE in intelligence.py), not something the
sales-tax closure monitor produces.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.comptroller.intelligence import SOURCE_TABLE_INTELLIGENCE, UnifiedIntelligenceItem
from app.comptroller.jurisdictions import Jurisdiction
from app.comptroller.property_adapter import get_property_adapter
from app.comptroller.service import _request_json, get_supabase_config, postgrest_headers


class NewAccountEnrichmentError(RuntimeError):
    pass


@dataclass(frozen=True)
class AppraiserAssignment:
    appraiser: str | None
    basis: str  # "tug" | "neighborhood" | "default" | "unassigned"
    reason: str


@dataclass(frozen=True)
class AccountCard:
    jurisdiction_id: str
    source_table: str
    item_id: str
    business_name: str | None
    legal_name: str | None
    source_address: str | None
    source_city: str | None
    source_state: str | None
    source_zip: str | None
    permit_start_date: str | None
    property_match_status: str | None
    situs_address: str | None
    real_account_number: str | None
    # Personal-property (BPP, 'P'-prefixed) account numbers Property
    # Enrichment found already on file at this address -- distinct from
    # real_account_number (always the 'R'-prefixed land record). A
    # non-empty list here is a strong "an account may already exist for
    # this business" signal, independent of whether name-matching found
    # anything (see app/comptroller/property_matching.classify_account_type).
    personal_property_accounts: list[str]
    tug: str | None
    neighborhood: str | None
    map_id: str | None
    appraiser_assignment: AppraiserAssignment
    suggested_property_link: str | None
    suggested_property_link_reason: str | None
    generated_at: str
    # Explicit reasons the card isn't fully automated for this item -- never
    # just a blank field (spec item 19: "automate the normal cases and make
    # the exceptions obvious"). Empty when everything resolved cleanly.
    exceptions: list[str]


def _normalize_neighborhood_code(value: str | None) -> str | None:
    """Lubbock's real NeighborhoodCode carries a base 4-digit neighborhood
    number plus, often, a suffix for sub-designations within it (e.g.
    '0718ARP2.RV5RV6', '0018CND1-4', '0204DM') -- confirmed against the
    real 234k-row export, where the same base number recurs with many
    different suffixes. The appraiser-assignment sheet only lists the base
    number ('for BPP we just need the first 4 digits, anything after does
    not matter'), so assignment must match on that prefix, not require an
    exact string match against the full code."""

    if not value:
        return None
    match = re.match(r"0*(\d+)", value.strip())
    if not match:
        return None
    return match.group(1).zfill(4)


def assign_appraiser(jurisdiction: Jurisdiction, *, tug: str | None, neighborhood: str | None) -> AppraiserAssignment:
    """TUG is checked before neighborhood -- it's the more specific unit in
    the pipeline this mirrors (PropertyID -> R account -> TUG -> Neighborhood
    -> Map). A jurisdiction that hasn't configured any rules yet (every
    jurisdiction, today -- no real Lubbock assignment data exists) always
    returns "unassigned" rather than guessing."""

    rules = jurisdiction.appraiser_assignment_rules or {}
    by_tug = rules.get("by_tug") or {}
    by_neighborhood = rules.get("by_neighborhood") or {}
    default = rules.get("default")

    if tug and tug in by_tug:
        return AppraiserAssignment(appraiser=by_tug[tug], basis="tug", reason=f"TUG {tug} is assigned to this appraiser.")

    normalized_neighborhood = _normalize_neighborhood_code(neighborhood)
    if normalized_neighborhood and normalized_neighborhood in by_neighborhood:
        return AppraiserAssignment(
            appraiser=by_neighborhood[normalized_neighborhood], basis="neighborhood",
            reason=f"Neighborhood {normalized_neighborhood} is assigned to this appraiser.",
        )
    if default:
        return AppraiserAssignment(appraiser=default, basis="default", reason="No TUG/neighborhood-specific rule matched; using the jurisdiction's default assignee.")
    return AppraiserAssignment(appraiser=None, basis="unassigned", reason="No appraiser-assignment rules configured for this jurisdiction yet.")


def _extract_personal_property_accounts(item: UnifiedIntelligenceItem) -> list[str]:
    """Reads the structured personal_property_accounts entry
    matching.build_signal_breakdown() writes into match_signals -- never
    parses the human-readable property_account prose string."""

    raw = (item.match_signals or {}).get("personal_property_accounts")
    if not raw or raw == "NONE FOUND":
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _compute_exceptions(item: UnifiedIntelligenceItem, assignment: AppraiserAssignment, personal_property_accounts: list[str]) -> list[str]:
    exceptions: list[str] = []

    if item.property_match_status == "AMBIGUOUS_PROPERTY_MATCH":
        exceptions.append(
            "PROPERTY MATCH AMBIGUOUS -- multiple candidate properties were found for this address; "
            "review the alternatives in Property Lookup before linking."
        )
    elif item.property_match_status in (None, "NO_PROPERTY_MATCH"):
        exceptions.append(
            "NO PROPERTY MATCH -- Property Enrichment did not find a matching real-property record; "
            "verify the source address or search manually."
        )
    elif not item.property_account_number:
        exceptions.append(
            "NO REAL ACCOUNT NUMBER -- a property record was found, but its R account/QuickRefID "
            "field is blank in the source data."
        )

    if personal_property_accounts:
        # Loud on purpose: a personal-property account already on file at
        # this exact address is the single strongest "this may not
        # actually be a new account" signal available before real rendition
        # data exists to name-match against.
        exceptions.append(
            "EXISTING P-ACCOUNT FOUND: " + ", ".join(personal_property_accounts) + " -- a business personal "
            "property account may already exist at this address; verify before creating a new one."
        )

    if assignment.basis == "unassigned":
        exceptions.append(
            "APPRAISER UNASSIGNED -- no matching TUG/neighborhood assignment rule is configured "
            "for this jurisdiction."
        )

    return exceptions


def build_account_card(item: UnifiedIntelligenceItem, jurisdiction: Jurisdiction) -> AccountCard:
    if item.signal_type != "new_business":
        raise NewAccountEnrichmentError(
            "Account cards are only produced for new-business intelligence items, not "
            f"'{item.signal_type}' items."
        )

    # Re-fetch the property record fresh (rather than trusting the item's
    # possibly-stale cached copy) when one was matched, the same way
    # property_enrichment.py rehydrates a cache hit.
    tug, neighborhood, map_id = item.tug, item.neighborhood, item.map_id
    property_record_id = item.raw.get("property_record_id")
    if property_record_id:
        adapter = get_property_adapter(jurisdiction)
        record = adapter.get_property_by_id(jurisdiction, property_record_id)
        if record is not None:
            tug, neighborhood, map_id = record.tug, record.neighborhood, record.map_id

    assignment = assign_appraiser(jurisdiction, tug=tug, neighborhood=neighborhood)
    personal_property_accounts = _extract_personal_property_accounts(item)
    exceptions = _compute_exceptions(item, assignment, personal_property_accounts)

    suggested_link = item.property_account_number
    suggested_link_reason = None
    if suggested_link:
        suggested_link_reason = f"{(item.property_match_status or '').replace('_', ' ').title()} to {item.matched_address}." if item.matched_address else "Matched by Property Enrichment."

    return AccountCard(
        jurisdiction_id=jurisdiction.id,
        source_table=item.source_table,
        item_id=item.id,
        business_name=item.business_name,
        legal_name=item.legal_name,
        source_address=item.source_address,
        source_city=item.source_city,
        source_state=item.source_state,
        source_zip=item.source_zip,
        permit_start_date=item.permit_start_date,
        property_match_status=item.property_match_status,
        situs_address=item.matched_address,
        real_account_number=item.property_account_number,
        personal_property_accounts=personal_property_accounts,
        tug=tug,
        neighborhood=neighborhood,
        map_id=map_id,
        appraiser_assignment=assignment,
        suggested_property_link=suggested_link,
        suggested_property_link_reason=suggested_link_reason,
        generated_at=datetime.now(timezone.utc).isoformat(),
        exceptions=exceptions,
    )


def mark_account_card_generated(item_id: str) -> None:
    """Advisory audit marker only -- see module docstring for why the card's
    content itself isn't separately persisted."""

    supabase_url, service_role_key = get_supabase_config()
    headers = postgrest_headers(service_role_key)
    _request_json(
        "PATCH",
        f"{supabase_url}/rest/v1/bpp_intelligence_items",
        headers,
        params={"id": f"eq.{item_id}"},
        json_payload={"account_card_generated_at": datetime.now(timezone.utc).isoformat()},
    )


def generate_account_card(item: UnifiedIntelligenceItem, jurisdiction: Jurisdiction, *, dry_run: bool = False) -> AccountCard:
    card = build_account_card(item, jurisdiction)
    if not dry_run and item.source_table == SOURCE_TABLE_INTELLIGENCE:
        mark_account_card_generated(item.id)
    return card


def account_card_to_dict(card: AccountCard) -> dict[str, Any]:
    return {
        "jurisdiction_id": card.jurisdiction_id,
        "source_table": card.source_table,
        "item_id": card.item_id,
        "business_name": card.business_name,
        "legal_name": card.legal_name,
        "source_address": card.source_address,
        "source_city": card.source_city,
        "source_state": card.source_state,
        "source_zip": card.source_zip,
        "permit_start_date": card.permit_start_date,
        "property_match_status": card.property_match_status,
        "situs_address": card.situs_address,
        "real_account_number": card.real_account_number,
        "personal_property_accounts": card.personal_property_accounts,
        "tug": card.tug,
        "neighborhood": card.neighborhood,
        "map_id": card.map_id,
        "appraiser": {
            "appraiser": card.appraiser_assignment.appraiser,
            "basis": card.appraiser_assignment.basis,
            "reason": card.appraiser_assignment.reason,
        },
        "suggested_property_link": card.suggested_property_link,
        "suggested_property_link_reason": card.suggested_property_link_reason,
        "generated_at": card.generated_at,
        "exceptions": card.exceptions,
    }
