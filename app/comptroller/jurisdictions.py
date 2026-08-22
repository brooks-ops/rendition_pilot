"""Jurisdiction abstraction: the first-class "which appraisal district/county
are we processing" concept every detection module and intelligence item is
scoped to.

RenditionPilot is not built only for Lubbock County. Lubbock is the first
*configured jurisdiction* and the reference implementation used to validate
the system -- it is not a special case baked into detection logic. Every
piece of Lubbock-specific configuration (its Comptroller county code, which
RenditionPilot district owns its account data, which intelligence modules it
has enabled, how its CAD data maps into the normalized account model) lives
in one `jurisdictions` row, not in code. Onboarding a second Texas county
should mean inserting a second row (plus, if its data source is shaped
differently, a new CadAdapter -- see cad_adapter.py) rather than new feature
development.

`comptroller_county_code`/`comptroller_dataset_id` replace the old pattern of
a hardcoded `TEXAS_COUNTY_CODES` dict + `COMPTROLLER_MONITORED_COUNTIES` env
var (still present in app/comptroller/counties.py for the already-deployed,
already-running sales-tax closure monitor -- see that module's docstring for
why it wasn't migrated onto this table in this pass). All NEW code
(New Business Detection and beyond) should take a `jurisdiction_id` and go
through this module, never a bare county name/code string.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.comptroller.service import _request_json, get_supabase_config, postgrest_headers

_SELECT = (
    "id,district_id,name,slug,county_name,state,timezone,active,"
    "comptroller_county_code,comptroller_dataset_id,capabilities,cad_field_mapping"
)


class JurisdictionError(RuntimeError):
    pass


@dataclass(frozen=True)
class Jurisdiction:
    id: str
    district_id: str | None
    name: str
    slug: str
    county_name: str
    state: str
    timezone: str
    active: bool
    comptroller_county_code: str | None
    comptroller_dataset_id: str
    capabilities: dict[str, bool] = field(default_factory=dict)
    cad_field_mapping: dict[str, str] = field(default_factory=dict)

    def has_capability(self, capability: str) -> bool:
        return bool(self.capabilities.get(capability))


def _row_to_jurisdiction(row: dict[str, Any]) -> Jurisdiction:
    return Jurisdiction(
        id=row["id"],
        district_id=row.get("district_id"),
        name=row["name"],
        slug=row["slug"],
        county_name=row["county_name"],
        state=row.get("state") or "TX",
        timezone=row.get("timezone") or "America/Chicago",
        active=bool(row.get("active", True)),
        comptroller_county_code=row.get("comptroller_county_code"),
        comptroller_dataset_id=row.get("comptroller_dataset_id") or "3kx8-uryv",
        capabilities=row.get("capabilities") or {},
        cad_field_mapping=row.get("cad_field_mapping") or {},
    )


def _fetch(params: dict[str, Any]) -> list[dict[str, Any]]:
    supabase_url, service_role_key = get_supabase_config()
    headers = postgrest_headers(service_role_key)
    rows = _request_json(
        "GET",
        f"{supabase_url}/rest/v1/jurisdictions",
        headers,
        params={"select": _SELECT, **params},
    )
    if not isinstance(rows, list):
        raise JurisdictionError(f"Unexpected response fetching jurisdictions: {rows!r}")
    return rows


def get_jurisdiction(jurisdiction_id: str) -> Jurisdiction:
    rows = _fetch({"id": f"eq.{jurisdiction_id}", "limit": "1"})
    if not rows:
        raise JurisdictionError(f"Jurisdiction '{jurisdiction_id}' was not found.")
    return _row_to_jurisdiction(rows[0])


def get_jurisdiction_by_slug(slug: str) -> Jurisdiction:
    rows = _fetch({"slug": f"eq.{slug}", "limit": "1"})
    if not rows:
        raise JurisdictionError(f"Jurisdiction '{slug}' was not found.")
    return _row_to_jurisdiction(rows[0])


def get_jurisdiction_by_county_name(county_name: str) -> Jurisdiction | None:
    rows = _fetch({"county_name": f"eq.{county_name}", "limit": "1"})
    return _row_to_jurisdiction(rows[0]) if rows else None


def list_active_jurisdictions(*, capability: str | None = None) -> list[Jurisdiction]:
    jurisdictions = [_row_to_jurisdiction(row) for row in _fetch({"active": "eq.true"})]
    if capability:
        jurisdictions = [j for j in jurisdictions if j.has_capability(capability)]
    return jurisdictions


# -- capability validation (spec item 11) ------------------------------------

# What a detection module needs from the normalized account model to run at
# all ("required") versus what would just improve its matching if available
# ("optional"). Compared against a CadAdapter's declared AVAILABLE_ACCOUNT_FIELDS
# (see cad_adapter.py) -- not against the jurisdiction row itself, since the
# same adapter/fields apply to every jurisdiction sharing one data source.
CAPABILITY_FIELD_REQUIREMENTS: dict[str, dict[str, list[str]]] = {
    "new_business_detection": {
        "required": ["owner_name"],
        "optional": ["situs_address", "dba_name", "mailing_address", "property_type"],
    },
    "sales_tax_monitoring": {
        "required": ["owner_name"],
        "optional": ["situs_address", "dba_name"],
    },
}

FIELD_LABELS: dict[str, str] = {
    "owner_name": "BPP owner name",
    "account_number": "BPP account number",
    "situs_address": "BPP situs address",
    "dba_name": "DBA name",
    "mailing_address": "mailing address",
    "property_type": "property type",
}


@dataclass(frozen=True)
class CapabilityValidation:
    ok: bool
    missing_required: list[str]
    missing_optional: list[str]
    message: str


def validate_capability(jurisdiction: Jurisdiction, capability: str, available_fields: frozenset[str]) -> CapabilityValidation:
    """Checked before any detection module runs -- see run_new_business_detection().

    Distinguishes "cannot run at all" (missing required fields, or the
    capability/county code isn't configured) from "can run, but with
    reduced matching power" (missing optional fields), per spec item 11.
    """

    label = capability.replace("_", " ").title()

    if not jurisdiction.has_capability(capability):
        return CapabilityValidation(
            ok=False, missing_required=[], missing_optional=[],
            message=f"{label} is not enabled for {jurisdiction.name}.",
        )

    if capability in ("new_business_detection", "sales_tax_monitoring") and not jurisdiction.comptroller_county_code:
        return CapabilityValidation(
            ok=False, missing_required=["comptroller_county_code"], missing_optional=[],
            message=(
                f"{label} cannot run for {jurisdiction.name}.\n\nMissing jurisdiction mappings:\n"
                "- Texas Comptroller county code"
            ),
        )

    requirements = CAPABILITY_FIELD_REQUIREMENTS.get(capability, {"required": [], "optional": []})
    missing_required = [f for f in requirements["required"] if f not in available_fields]
    missing_optional = [f for f in requirements["optional"] if f not in available_fields]

    if missing_required:
        labels = "\n".join(f"- {FIELD_LABELS.get(f, f)}" for f in missing_required)
        return CapabilityValidation(
            ok=False, missing_required=missing_required, missing_optional=missing_optional,
            message=f"{label} cannot run.\n\nMissing jurisdiction mappings:\n{labels}",
        )

    if missing_optional:
        labels = "\n".join(f"- {FIELD_LABELS.get(f, f)}" for f in missing_optional)
        return CapabilityValidation(
            ok=True, missing_required=[], missing_optional=missing_optional,
            message=f"{label} available with reduced matching capability.\n\nOptional field(s) unavailable:\n{labels}",
        )

    return CapabilityValidation(ok=True, missing_required=[], missing_optional=[], message=f"{label} fully available.")
