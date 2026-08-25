"""Production readiness diagnostic: for one jurisdiction, which pipeline
dependencies are actually satisfied right now.

Deliberately NOT a single red/green flag -- "New Business Detection: READY"
can still mean name-only matching while "high-confidence corroboration" is
separately BLOCKED for a different, specific reason. Each check reports its
own status and a plain-English reason, so a missing dependency is obvious
without having to read code or query the database by hand.

Read-only. Existence checks use `limit=1` (not exact counts) -- this is a
diagnostic, not a reporting/analytics feature.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.comptroller.jurisdictions import Jurisdiction, validate_capability
from app.comptroller.service import _request_json, get_supabase_config, postgrest_headers

READY = "READY"
NOT_READY = "NOT_READY"
BLOCKED = "BLOCKED"
OPTIONAL = "OPTIONAL"
NOT_CONFIGURED = "NOT_CONFIGURED"
DEGRADED = "DEGRADED"


@dataclass(frozen=True)
class ReadinessCheck:
    name: str
    status: str
    detail: str


@dataclass(frozen=True)
class ProductionReadiness:
    jurisdiction_id: str
    jurisdiction_name: str
    checks: list[ReadinessCheck]


def _has_any_row(table: str, params: dict[str, str]) -> bool:
    supabase_url, service_role_key = get_supabase_config()
    headers = postgrest_headers(service_role_key)
    rows = _request_json(
        "GET", f"{supabase_url}/rest/v1/{table}", headers,
        params={"select": "id", "limit": "1", **params},
    )
    return isinstance(rows, list) and len(rows) > 0


def assess_production_readiness(jurisdiction: Jurisdiction) -> ProductionReadiness:
    checks: list[ReadinessCheck] = []

    # Comptroller data
    has_comptroller_data = bool(jurisdiction.comptroller_county_code) and _has_any_row(
        "comptroller_permit_locations", {"county": f"eq.{jurisdiction.county_name}"}
    )
    checks.append(ReadinessCheck(
        "Comptroller data", READY if has_comptroller_data else NOT_READY,
        "Sales-tax permit data has been synced for this county." if has_comptroller_data
        else "No comptroller_permit_locations rows for this county -- run baseline/sync first.",
    ))

    # Persisted BPP accounts (parsed_rendition_results)
    has_persisted_accounts = bool(jurisdiction.district_id) and _has_any_row(
        "parsed_rendition_results", {"district_id": f"eq.{jurisdiction.district_id}"}
    )
    checks.append(ReadinessCheck(
        "Persisted BPP accounts", READY if has_persisted_accounts else NOT_READY,
        "At least one locked rendition review has been persisted." if has_persisted_accounts
        else "No parsed_rendition_results rows for this district yet -- lock a review while signed in to persist one.",
    ))

    # Property data
    has_property_data = _has_any_row("real_property_records", {"jurisdiction_id": f"eq.{jurisdiction.id}"})
    checks.append(ReadinessCheck(
        "Property data", READY if has_property_data else NOT_READY,
        "At least one real-property record has been imported." if has_property_data
        else "No real_property_records rows -- run property-import with a county export.",
    ))

    # Property field mapping
    mapping_validation = validate_capability(
        jurisdiction, "real_property_linkage", frozenset(jurisdiction.property_field_mapping.keys())
    )
    if not jurisdiction.has_capability("real_property_linkage"):
        mapping_status, mapping_detail = NOT_CONFIGURED, "real_property_linkage capability is disabled for this jurisdiction."
    elif mapping_validation.ok and not mapping_validation.missing_optional:
        mapping_status, mapping_detail = READY, "All property fields are mapped."
    elif mapping_validation.ok:
        mapping_status, mapping_detail = DEGRADED, mapping_validation.message
    else:
        mapping_status, mapping_detail = NOT_READY, mapping_validation.message
    checks.append(ReadinessCheck("Property field mapping", mapping_status, mapping_detail))

    # Current tax year
    checks.append(ReadinessCheck(
        "Current tax year", READY if jurisdiction.current_tax_year else OPTIONAL,
        f"Matching prefers tax year {jurisdiction.current_tax_year}." if jurisdiction.current_tax_year
        else "Not set -- matching falls back to the newest available year per property (not blocking).",
    ))

    # Appraiser assignment rules
    has_rules = bool(jurisdiction.appraiser_assignment_rules)
    checks.append(ReadinessCheck(
        "Appraiser rules", READY if has_rules else NOT_CONFIGURED,
        "TUG/neighborhood/default assignment rules are configured." if has_rules
        else "No appraiser_assignment_rules configured -- every account card will show UNASSIGNED (not blocking).",
    ))

    # New Business Detection (name-only always works if the capability + county code exist)
    nbd_ready = jurisdiction.has_capability("new_business_detection") and bool(jurisdiction.comptroller_county_code)
    checks.append(ReadinessCheck(
        "New Business Detection", READY if nbd_ready else BLOCKED,
        "Name-only matching is available." if nbd_ready
        else "new_business_detection capability disabled or no Comptroller county code configured.",
    ))
    checks.append(ReadinessCheck(
        "High-confidence account corroboration",
        READY if (nbd_ready and has_persisted_accounts and has_property_data) else BLOCKED,
        "Name + property + account-number corroboration can all be evaluated." if (nbd_ready and has_persisted_accounts and has_property_data)
        else "BLOCKED: " + (
            "no persisted BPP accounts" if not has_persisted_accounts
            else "no property data imported" if not has_property_data
            else "New Business Detection itself is not ready"
        ),
    ))

    # Property Enrichment
    property_enrichment_ready = jurisdiction.has_capability("real_property_linkage") and mapping_validation.ok and has_property_data
    checks.append(ReadinessCheck(
        "Property Enrichment", READY if property_enrichment_ready else BLOCKED,
        "Address matching against real property records is available." if property_enrichment_ready
        else "BLOCKED: " + (
            "real_property_linkage capability disabled" if not jurisdiction.has_capability("real_property_linkage")
            else mapping_validation.message if not mapping_validation.ok
            else "no property data imported"
        ),
    ))

    # Account Card
    account_card_ready = nbd_ready  # the card itself always renders; completeness depends on the checks above
    checks.append(ReadinessCheck(
        "Account Card", READY if account_card_ready else BLOCKED,
        "Cards generate for new-business items; missing property/appraiser data surfaces as explicit exceptions on the card, not a blocked feature."
        if account_card_ready else "BLOCKED: New Business Detection is not ready, so no new-business items exist to card.",
    ))

    # Mailing Address Intelligence
    mailing_capability_ready = jurisdiction.has_capability("mailing_address_monitoring") and bool(jurisdiction.comptroller_county_code)
    checks.append(ReadinessCheck(
        "Mailing Address Monitoring", READY if mailing_capability_ready else BLOCKED,
        "Comptroller-vs-own-history comparison is available." if mailing_capability_ready
        else "mailing_address_monitoring capability disabled or no Comptroller county code configured.",
    ))

    has_mailing_observations = _has_any_row("mailing_address_observations", {"jurisdiction_id": f"eq.{jurisdiction.id}"})
    checks.append(ReadinessCheck(
        "Current CAD mailing addresses", READY if has_mailing_observations else NOT_READY,
        "At least one taxpayer mailing address has been observed and recorded." if has_mailing_observations
        else "No mailing_address_observations rows yet -- run mailing-address-scan to establish a baseline.",
    ))

    checks.append(ReadinessCheck(
        "Rendition mailing addresses", NOT_READY,
        "The rendition OCR pipeline does not currently extract a mailing address from the form -- "
        "this comparison path is built (record_observation/compare_against_latest_observation both "
        "accept account_identifier_type='bpp_account') but has no real data to run against yet. "
        "See docs/mailing_address_intelligence.md.",
    ))

    checks.append(ReadinessCheck(
        "Other mailing sources", NOT_CONFIGURED,
        "None configured beyond the Texas Comptroller feed.",
    ))

    checks.append(ReadinessCheck("Comparison engine", READY, "compare_mailing_addresses() is available and tested."))

    checks.append(ReadinessCheck(
        "Automatic rendition trigger", "DISABLED",
        "Locking a rendition does not yet trigger a mailing-address comparison -- run mailing-address-scan "
        "manually or via the run-intelligence cron dispatcher instead (deliberately not wired into the "
        "review/lock request path, to avoid adding latency there).",
    ))

    checks.append(ReadinessCheck(
        "Review queue", READY,
        "mailing_address_change items appear in the existing BPP Intelligence Queue.",
    ))

    return ProductionReadiness(jurisdiction_id=jurisdiction.id, jurisdiction_name=jurisdiction.name, checks=checks)


def format_readiness_report(readiness: ProductionReadiness) -> str:
    lines = [f"Production Readiness -- {readiness.jurisdiction_name}", ""]
    for check in readiness.checks:
        lines.append(f"{check.name}: {check.status}")
        lines.append(f"  {check.detail}")
    return "\n".join(lines)
