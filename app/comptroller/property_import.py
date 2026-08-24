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
import time
from dataclasses import dataclass
from io import BytesIO, StringIO

import requests

from app.comptroller.jurisdictions import Jurisdiction, validate_capability
from app.comptroller.property_adapter import normalize_source_record
from app.comptroller.property_enrichment import CAPABILITY
from app.comptroller.service import ComptrollerServiceError, _request_json, get_supabase_config, postgrest_headers

_UPSERT_MAX_ATTEMPTS = 4
_UPSERT_RETRY_BACKOFF_SECONDS = 2.0


class PropertyImportError(RuntimeError):
    pass


@dataclass(frozen=True)
class PropertyImportResult:
    jurisdiction_id: str
    rows_read: int
    rows_imported: int
    rows_skipped: int
    import_id: str | None
    # Source rows sharing the same (source_property_id, tax_year) -- the
    # last one in the file wins, all others are collapsed rather than
    # silently dropped. Real Lubbock 2027 data has ~11k such rows (the same
    # PropertyID/QuickRefID repeated across many individually-addressed
    # platted lots) -- reported explicitly, never silently discarded.
    rows_deduplicated: int = 0


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
    # Must match real_property_records_upsert_unique_idx exactly (see
    # supabase/migrations/20260825_fix_real_property_records_upsert_constraint.sql)
    # -- PostgREST's on_conflict only matches a NON-partial unique
    # index/constraint on precisely this column list.
    #
    # Retries transient network/TLS errors (found for real importing a
    # 240k-row Lubbock export over several hundred sequential batches --
    # one batch hit a mid-stream SSL error; everything before it had
    # already committed safely). The upsert is idempotent by key, so
    # retrying (or re-running the whole import) is always safe -- it never
    # duplicates a row.
    last_error: requests.exceptions.RequestException | None = None
    for attempt in range(1, _UPSERT_MAX_ATTEMPTS + 1):
        try:
            _request_json(
                "POST",
                f"{_base_url()}/rest/v1/real_property_records",
                _headers(prefer="resolution=merge-duplicates,return=minimal"),
                params={"on_conflict": "jurisdiction_id,source_property_id,tax_year"},
                json_payload=rows,
            )
            return
        except requests.exceptions.RequestException as exc:
            last_error = exc
            if attempt < _UPSERT_MAX_ATTEMPTS:
                time.sleep(_UPSERT_RETRY_BACKOFF_SECONDS * attempt)
    raise PropertyImportError(f"Upserting a batch of {len(rows)} property rows failed after {_UPSERT_MAX_ATTEMPTS} attempts: {last_error}")


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
    rows_deduplicated = 0
    # Collected (not streamed) before upserting: a single upsert statement
    # can't target the same conflict key twice ("ON CONFLICT DO UPDATE
    # command cannot affect row a second time"), so every row sharing a
    # (source_property_id, tax_year) key must collapse to one BEFORE
    # batching, not just within whichever batch they happen to land in.
    deduped: dict[tuple, dict] = {}

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
        key = (normalized["source_property_id"], normalized["tax_year"])
        if key in deduped:
            rows_deduplicated += 1
        deduped[key] = normalized  # last occurrence in the file wins

    rows = list(deduped.values())
    imported = 0
    if dry_run:
        imported = len(rows)
    else:
        for offset in range(0, len(rows), batch_size):
            batch = rows[offset:offset + batch_size]
            _upsert_property_rows(batch)
            imported += len(batch)

    if not dry_run and import_id:
        _finalize_import_record(import_id, imported)

    return PropertyImportResult(
        jurisdiction_id=jurisdiction.id,
        rows_read=rows_read,
        rows_imported=imported,
        rows_skipped=rows_skipped,
        rows_deduplicated=rows_deduplicated,
        import_id=import_id,
    )


def excel_bytes_to_csv_text(file_bytes: bytes, *, sheet_name: str | int = 0) -> str:
    """Converts one sheet of an .xlsx/.xls export to the same CSV text
    `import_property_csv` already handles -- pandas/openpyxl are existing
    dependencies (already used by app/comptroller/export.py), so Excel
    support is one small conversion step, not a second import path."""

    import pandas as pd

    frame = pd.read_excel(BytesIO(file_bytes), sheet_name=sheet_name, dtype=str)
    return frame.to_csv(index=False)


def import_property_file(
    jurisdiction: Jurisdiction,
    file_bytes: bytes,
    filename: str,
    *,
    source_as_of_date: str | None = None,
    notes: str | None = None,
    dry_run: bool = False,
) -> PropertyImportResult:
    """Dispatches to import_property_csv by file extension -- .csv is read
    as-is; .xlsx/.xls are converted via excel_bytes_to_csv_text() first.
    Same generic import path either way; no per-format-specific behavior
    beyond this one conversion step."""

    lower = filename.lower()
    if lower.endswith(".csv"):
        csv_text = file_bytes.decode("utf-8-sig")
    elif lower.endswith((".xlsx", ".xls")):
        csv_text = excel_bytes_to_csv_text(file_bytes)
    else:
        raise PropertyImportError(f"Unsupported property export file type: '{filename}'. Use .csv or .xlsx.")
    return import_property_csv(
        jurisdiction, csv_text, source_as_of_date=source_as_of_date, notes=notes, dry_run=dry_run,
    )
