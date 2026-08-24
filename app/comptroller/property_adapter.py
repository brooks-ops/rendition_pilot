"""Property source adapter: the normalized real-property model plus the one
adapter implementation every jurisdiction uses today.

Mirrors cad_adapter.py's shape deliberately. Just like RenditionPilotCadAdapter
is the one real CadAdapter because every jurisdiction's BPP data lives in the
same Supabase project, ImportedPropertyAdapter is the one real PropertyAdapter
because every jurisdiction's property data arrives the same way: a file
export with county-specific column names, mapped at import time (see
property_import.py) into `real_property_records` via
`jurisdiction.property_field_mapping`. A county with a *live* CRS database
connection instead of a file export would get a second adapter class,
selected by `get_property_adapter()` -- not a branch inside the matching
engine. No such adapter exists yet because no jurisdiction has one.

County-specific column names (PropertyID, QuickRefID, TUG, NBHD, ... for
Lubbock; ParcelKey, AccountRef, ... for some other county) never appear
below this module -- normalize_source_record() is the only place they are
read, and only via the jurisdiction's own field mapping.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.comptroller.jurisdictions import Jurisdiction
from app.comptroller.service import ComptrollerServiceError, _request_json, get_supabase_config, postgrest_headers


class PropertySourceError(RuntimeError):
    pass


@dataclass(frozen=True)
class NormalizedRealProperty:
    property_id: str
    jurisdiction_id: str
    source_property_id: str
    tax_year: int | None
    real_account_number: str | None
    situs_address_raw: str | None
    situs_address_normalized: str | None
    situs_city: str | None
    situs_state: str | None
    situs_zip: str | None
    owner_name: str | None
    tug: str | None
    neighborhood: str | None
    map_id: str | None
    latitude: float | None
    longitude: float | None
    source_system: str | None
    source_import_id: str | None
    source_updated_at: str | None


_SELECT = (
    "id,jurisdiction_id,source_property_id,tax_year,real_account_number,situs_address_raw,"
    "situs_address_normalized,situs_city,situs_state,situs_zip,owner_name,tug,"
    "neighborhood,map_id,latitude,longitude,source_system,source_import_id,source_updated_at"
)


def _row_to_property(row: dict[str, Any]) -> NormalizedRealProperty:
    return NormalizedRealProperty(
        property_id=row["id"],
        jurisdiction_id=row["jurisdiction_id"],
        source_property_id=row["source_property_id"],
        tax_year=row.get("tax_year"),
        real_account_number=row.get("real_account_number"),
        situs_address_raw=row.get("situs_address_raw"),
        situs_address_normalized=row.get("situs_address_normalized"),
        situs_city=row.get("situs_city"),
        situs_state=row.get("situs_state"),
        situs_zip=row.get("situs_zip"),
        owner_name=row.get("owner_name"),
        tug=row.get("tug"),
        neighborhood=row.get("neighborhood"),
        map_id=row.get("map_id"),
        latitude=row.get("latitude"),
        longitude=row.get("longitude"),
        source_system=row.get("source_system"),
        source_import_id=row.get("source_import_id"),
        source_updated_at=row.get("source_updated_at"),
    )


def normalize_source_record(
    raw_row: dict[str, Any],
    field_mapping: dict[str, str],
    *,
    jurisdiction_id: str,
    source_system: str = "imported_file",
    source_import_id: str | None = None,
) -> dict[str, Any] | None:
    """Map one raw export row into a `real_property_records` insert payload
    using `field_mapping` (normalized field -> raw column name). Returns
    None if the row has no usable `source_property_id` (the one field every
    downstream lookup keys off of). Never reads a raw column name that
    wasn't supplied in `field_mapping` -- this is the only place a county's
    own schema is touched."""

    from app.comptroller.address_normalizer import normalize_address

    def raw(field: str) -> Any:
        column = field_mapping.get(field)
        return raw_row.get(column) if column else None

    source_property_id = raw("source_property_id")
    if not source_property_id:
        return None

    situs_raw = raw("situs_address")
    situs_zip = raw("situs_zip")
    normalized = normalize_address(situs_raw, zip_code=situs_zip) if situs_raw else None

    raw_tax_year = raw("tax_year")
    try:
        tax_year = int(raw_tax_year) if raw_tax_year not in (None, "") else None
    except (TypeError, ValueError):
        tax_year = None

    return {
        "jurisdiction_id": jurisdiction_id,
        "source_property_id": str(source_property_id),
        "tax_year": tax_year,
        "real_account_number": raw("real_account_number"),
        "situs_address_raw": situs_raw,
        "situs_address_normalized": normalized.normalized if normalized else None,
        "situs_city": raw("situs_city"),
        "situs_state": raw("situs_state"),
        "situs_zip": situs_zip,
        "owner_name": raw("owner_name"),
        "tug": raw("tug"),
        "neighborhood": raw("neighborhood"),
        "map_id": raw("map_id"),
        "latitude": raw("latitude"),
        "longitude": raw("longitude"),
        "source_system": source_system,
        "source_import_id": source_import_id,
    }


def _select_preferred_year(
    properties: list[NormalizedRealProperty], jurisdiction: Jurisdiction
) -> list[NormalizedRealProperty]:
    """Collapse multiple tax-year rows for the same underlying property down
    to one, per spec item 5's preferred behavior: the jurisdiction's
    configured `current_tax_year` if a row for that year exists, else the
    newest available year, else whatever single undated row exists. Every
    year's row still exists in `real_property_records` for auditability --
    this only affects which one a live lookup/match sees."""

    if not properties:
        return properties
    groups: dict[str, list[NormalizedRealProperty]] = {}
    for prop in properties:
        groups.setdefault(prop.source_property_id, []).append(prop)

    selected: list[NormalizedRealProperty] = []
    for group in groups.values():
        if len(group) == 1:
            selected.append(group[0])
            continue
        preferred = None
        if jurisdiction.current_tax_year is not None:
            preferred = next((p for p in group if p.tax_year == jurisdiction.current_tax_year), None)
        if preferred is None:
            dated = [p for p in group if p.tax_year is not None]
            preferred = max(dated, key=lambda p: p.tax_year) if dated else group[0]
        selected.append(preferred)
    return selected


class ImportedPropertyAdapter:
    """Reads already-normalized `real_property_records` rows for a
    jurisdiction. See module docstring: this is the one adapter every
    jurisdiction uses, because county-specific translation happens once, at
    import time, not per-query."""

    def _fetch(self, jurisdiction: Jurisdiction, params: dict[str, Any]) -> list[NormalizedRealProperty]:
        try:
            supabase_url, service_role_key = get_supabase_config()
        except ComptrollerServiceError as exc:
            raise PropertySourceError(str(exc)) from exc
        headers = postgrest_headers(service_role_key)
        rows = _request_json(
            "GET",
            f"{supabase_url}/rest/v1/real_property_records",
            headers,
            params={"select": _SELECT, "jurisdiction_id": f"eq.{jurisdiction.id}", **params},
        )
        if not isinstance(rows, list):
            raise PropertySourceError(f"Unexpected response fetching real_property_records: {rows!r}")
        return [_row_to_property(row) for row in rows]

    def _fetch_all(self, jurisdiction: Jurisdiction, *, page_size: int = 2000) -> list[NormalizedRealProperty]:
        """Paginates through every real_property_records row for a
        jurisdiction (spec item 22: a real county export can be hundreds of
        thousands of rows, including every prior year's history). Uses this
        module's own `_request_json` binding directly (rather than
        service._paginated_get, which would call service.py's own separate
        binding) so tests that monkeypatch property_adapter._request_json
        still intercept it -- see tests/comptroller_fakes.py convention."""

        try:
            supabase_url, service_role_key = get_supabase_config()
        except ComptrollerServiceError as exc:
            raise PropertySourceError(str(exc)) from exc
        headers = postgrest_headers(service_role_key)
        base_params = {"select": _SELECT, "jurisdiction_id": f"eq.{jurisdiction.id}"}
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = _request_json(
                "GET",
                f"{supabase_url}/rest/v1/real_property_records",
                headers,
                params={**base_params, "limit": page_size, "offset": offset},
            )
            if not isinstance(page, list):
                raise PropertySourceError(f"Unexpected response fetching real_property_records: {page!r}")
            rows.extend(page)
            if len(page) < page_size:
                break
            offset += page_size
        return [_row_to_property(row) for row in rows]

    def get_property_by_id(self, jurisdiction: Jurisdiction, property_id: str) -> NormalizedRealProperty | None:
        rows = self._fetch(jurisdiction, {"id": f"eq.{property_id}", "limit": "1"})
        return rows[0] if rows else None

    def find_properties_by_address(
        self, jurisdiction: Jurisdiction, normalized_base_address: str, *, limit: int = 50
    ) -> list[NormalizedRealProperty]:
        if not normalized_base_address:
            return []
        # PostgREST ilike wildcard match on the normalized column -- the
        # unit suffix (if any) lives in the raw address, not this column, so
        # this intentionally returns every unit at a base address and lets
        # property_matching.py's suite logic disambiguate. A higher fetch
        # limit than the caller asked for, since multiple tax-year rows for
        # the same unit are collapsed to one below -- fetching exactly
        # `limit` raw rows could under-return distinct properties.
        rows = self._fetch(
            jurisdiction,
            {"situs_address_normalized": f"ilike.{normalized_base_address}*", "limit": str(limit * 4)},
        )
        return _select_preferred_year(rows, jurisdiction)[:limit]

    def find_properties_by_real_account(self, jurisdiction: Jurisdiction, account_number: str) -> list[NormalizedRealProperty]:
        if not account_number:
            return []
        rows = self._fetch(jurisdiction, {"real_account_number": f"eq.{account_number}"})
        return _select_preferred_year(rows, jurisdiction)

    def search_properties(self, jurisdiction: Jurisdiction, *, limit: int | None = None) -> list[NormalizedRealProperty]:
        """Every property record for a jurisdiction (one row per property,
        newest/current tax year -- see _select_preferred_year) -- used for
        batch enrichment and the portability test, not for interactive
        lookups (use find_properties_by_address for those).

        Paginates rather than relying on a single request's `limit` (spec
        item 22: a real county export can be hundreds of thousands of rows,
        including every prior year's history) -- unbounded by default; pass
        `limit` to cap it (e.g. for a quick manual check)."""

        properties = self._fetch(jurisdiction, {"limit": str(limit)}) if limit is not None else self._fetch_all(jurisdiction)
        return _select_preferred_year(properties, jurisdiction)


def get_property_adapter(jurisdiction: Jurisdiction) -> ImportedPropertyAdapter:
    return ImportedPropertyAdapter()
