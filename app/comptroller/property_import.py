"""Import a county property/CRS export (CSV) into `real_property_records`,
normalizing county-specific column names via `jurisdiction.property_field_mapping`.

This is the file-import path spec item 6 asks for: RenditionPilot has no
live database connection to any CAD's CRS system, so every jurisdiction's
property data arrives as an export file, gets field-mapped once here, and
every other module only ever sees the normalized `real_property_records`
shape. Onboarding a second county's property data means adding a mapping
config and running this import -- no new adapter class, no code change.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from io import StringIO

from app.comptroller.jurisdictions import Jurisdiction, validate_capability
from app.comptroller.property_adapter import normalize_source_record
from app.comptroller.property_enrichment import CAPABILITY
from app.comptroller.service import ComptrollerServiceError, _request_json, get_supabase_config, postgrest_headers


class PropertyImportError(RuntimeError):
    pass


@dataclass(frozen=True)
class PropertyImportResult:
    jurisdiction_id: str
    rows_read: int
    rows_imported: int
    rows_skipped: int
    import_id: str | None


def _headers(prefer: str | None = None) -> dict[str, str]:
    try:
        _, service_role_key = get_supabase_config()
    except ComptrollerServiceError as exc:
        raise PropertyImportError(str(exc)) from exc
    return postgrest_headers(service_role_key, prefer=prefer)


def _base_url() -> str:
    supabase_url, _ = get_supabase_config()
    return supabase_url


def _create_import_record(jurisdiction_id: str, *, source_as_of_date: str | None, notes: str | None) -> str:
    rows = _request_json(
        "POST",
        f"{_base_url()}/rest/v1/property_source_imports",
        _headers(prefer="return=representation"),
        json_payload=[{
            "jurisdiction_id": jurisdiction_id,
            "source_as_of_date": source_as_of_date,
            "notes": notes,
            "row_count": 0,
        }],
    )
    if not isinstance(rows, list) or not rows:
        raise PropertyImportError(f"Unexpected response creating property_source_imports row: {rows!r}")
    return rows[0]["id"]


def _finalize_import_record(import_id: str, row_count: int) -> None:
    _request_json(
        "PATCH",
        f"{_base_url()}/rest/v1/property_source_imports",
        _headers(),
        params={"id": f"eq.{import_id}"},
        json_payload={"row_count": row_count},
    )


def _upsert_property_rows(rows: list[dict]) -> None:
    if not rows:
        return
    _request_json(
        "POST",
        f"{_base_url()}/rest/v1/real_property_records",
        _headers(prefer="resolution=merge-duplicates,return=minimal"),
        params={"on_conflict": "jurisdiction_id,source_property_id"},
        json_payload=rows,
    )


def import_property_csv(
    jurisdiction: Jurisdiction,
    csv_text: str,
    *,
    source_as_of_date: str | None = None,
    notes: str | None = None,
    dry_run: bool = False,
    batch_size: int = 500,
) -> PropertyImportResult:
    validation = validate_capability(
        jurisdiction, CAPABILITY, frozenset(jurisdiction.property_field_mapping.keys())
    )
    if not validation.ok:
        raise PropertyImportError(validation.message)

    reader = csv.DictReader(StringIO(csv_text))
    import_id = None if dry_run else _create_import_record(
        jurisdiction.id, source_as_of_date=source_as_of_date, notes=notes
    )

    rows_read = 0
    rows_skipped = 0
    batch: list[dict] = []
    imported = 0

    for raw_row in reader:
        rows_read += 1
        normalized = normalize_source_record(
            raw_row,
            jurisdiction.property_field_mapping,
            jurisdiction_id=jurisdiction.id,
            source_import_id=import_id,
        )
        if normalized is None:
            rows_skipped += 1
            continue
        batch.append(normalized)
        if not dry_run and len(batch) >= batch_size:
            _upsert_property_rows(batch)
            imported += len(batch)
            batch = []
        elif dry_run:
            imported += 1

    if not dry_run and batch:
        _upsert_property_rows(batch)
        imported += len(batch)

    if not dry_run and import_id:
        _finalize_import_record(import_id, imported)

    return PropertyImportResult(
        jurisdiction_id=jurisdiction.id,
        rows_read=rows_read,
        rows_imported=imported,
        rows_skipped=rows_skipped,
        import_id=import_id,
    )
