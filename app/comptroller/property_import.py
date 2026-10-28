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
import re
from datetime import datetime, timezone
import time
from dataclasses import dataclass, field
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
    quality_summary: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


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
            "import_status": "IN_PROGRESS",
        }],
    )
    if not isinstance(rows, list) or not rows:
        raise PropertyImportError(f"Unexpected response creating property_source_imports row: {rows!r}")
    return rows[0]["id"]


def _finalize_import_record(import_id: str, row_count: int, quality: dict) -> None:
    _request_json(
        "PATCH",
        f"{_base_url()}/rest/v1/property_source_imports",
        _headers(),
        params={"id": f"eq.{import_id}"},
        json_payload={"row_count": row_count, "quality_summary": quality, "import_status": "COMPLETED",
                      "completed_at": datetime.now(timezone.utc).isoformat()},
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
    columns = reader.fieldnames or []
    validate_mapping(columns, jurisdiction.property_field_mapping)
    import_id = None

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
    quality = import_quality(rows, jurisdiction)
    warnings = quality["warnings"]
    # A sparse export must not erase identity learned from a richer export.
    for row in rows:
        for key in ("owner_name", "business_name", "dba_name", "account_status", "situs_unit", "source_updated_at"):
            if not row.get(key):
                row.pop(key, None)
    imported = 0
    if dry_run:
        imported = len(rows)
    else:
        if not rows:
            raise PropertyImportError("No usable property records were found; nothing was imported.")
        import_id = _create_import_record(jurisdiction.id, source_as_of_date=source_as_of_date, notes=notes)
        try:
            # PostgREST batches require identical object keys. Group sparse
            # identity fields so missing names preserve previous values.
            groups = {}
            for row in rows:
                row["source_import_id"] = import_id
                groups.setdefault(tuple(sorted(row)), []).append(row)
            for group in groups.values():
                for offset in range(0, len(group), batch_size):
                    batch = group[offset:offset + batch_size]
                    _upsert_property_rows(batch)
                    imported += len(batch)
            _finalize_import_record(import_id, imported, quality)
        except Exception:
            _request_json("PATCH", f"{_base_url()}/rest/v1/property_source_imports", _headers(),
                          params={"id": f"eq.{import_id}"}, json_payload={"row_count": imported, "import_status": "FAILED"})
            raise

    return PropertyImportResult(
        jurisdiction_id=jurisdiction.id,
        rows_read=rows_read,
        rows_imported=imported,
        rows_skipped=rows_skipped,
        rows_deduplicated=rows_deduplicated,
        import_id=import_id,
        quality_summary=quality, warnings=warnings,
    )


def excel_bytes_to_csv_text(file_bytes: bytes, *, sheet_name: str | int = 0) -> str:
    """Converts one sheet of an .xlsx/.xls export to the same CSV text
    `import_property_csv` already handles -- pandas/openpyxl are existing
    dependencies (already used by app/comptroller/export.py), so Excel
    support is one small conversion step, not a second import path."""

    if file_bytes.startswith(b"PK"):
        import openpyxl
        workbook = openpyxl.load_workbook(BytesIO(file_bytes), read_only=True, data_only=True)
        try:
            sheet = workbook.worksheets[sheet_name] if isinstance(sheet_name, int) else workbook[sheet_name]
            output = StringIO()
            writer = csv.writer(output)
            for values in sheet.iter_rows(values_only=True):
                writer.writerow([int(v) if isinstance(v, float) and v.is_integer() else v for v in values])
            return output.getvalue()
        finally:
            workbook.close()
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


IMPORT_FIELDS = {
    'source_property_id': ['PropertyID','PropertyId','PropertyKey','ParcelID'],
    'real_account_number': ['QuickRefID','AccountNumber','AccountNo','Account','AccountID','Account #','Property Number'],
    'tax_year': ['AdHocTaxYear','TaxYear'],
    'situs_address': ['SitusAddress','PropertyAddress','PhysicalAddress','LocationAddress','Situs','Address'],
    'situs_city': ['SitusCity'], 'situs_state': ['SitusState'], 'situs_zip': ['SitusZip','Zip','SitusZipCode'],
    'owner_name': ['OwnerName','Owner','LegalName'], 'business_name': ['BusinessName'],
    'dba_name': ['DBA','DBAName','DoingBusinessAs'], 'situs_unit': ['SitusUnit','Suite','Unit'],
    'account_status': ['AccountStatus','PropertyStatus'], 'source_updated_at': ['SourceUpdatedAt'],
    'tug': ['TaxingUnitGroupCode','TUG','TaxingUnitGroupID'],
    'neighborhood': ['NeighborhoodCode','NBHD'], 'map_id': ['MapNumber','MapID'],
}


def suggest_mapping(columns: list[str], configured: dict[str,str]) -> dict[str,str]:
    def key(value):return re.sub(r'[^a-z0-9]', '', value.lower())
    lookup = {key(c): c for c in columns}
    result = {}
    for field_name, aliases in IMPORT_FIELDS.items():
        if configured.get(field_name) in columns:
            result[field_name] = configured[field_name]
        else:
            matches = [lookup[key(a)] for a in [field_name, *aliases] if key(a) in lookup]
            if matches: result[field_name] = matches[0]
    if not result.get("source_property_id") and result.get("real_account_number"):
        result["source_property_id"]=result["real_account_number"]
    return result


def validate_mapping(columns, mapping):
    for key in ('source_property_id', 'real_account_number', 'situs_address'):
        if not mapping.get(key):
            raise PropertyImportError(f'Map the {key.replace("_", " ")} column before importing.')
    for field_name, column in mapping.items():
        if field_name not in IMPORT_FIELDS and field_name not in ('latitude','longitude'):
            raise PropertyImportError(f'Unsupported field: {field_name}.')
        if column not in columns:
            raise PropertyImportError(f'Mapped column "{column}" is missing from this file.')


def import_quality(rows, jurisdiction):
    from app.comptroller.property_matching import classify_account_type
    personal = [r for r in rows if classify_account_type(r.get('real_account_number'), prefixes=jurisdiction.account_type_prefixes) == 'PERSONAL']
    named = sum(bool(r.get('owner_name') or r.get('business_name') or r.get('dba_name')) for r in personal)
    warnings = []
    if personal and named < len(personal):warnings.append(f'{len(personal)-named:,} of {len(personal):,} BPP account records have no owner, business, or DBA name; those matches need manual verification.')
    if any(not r.get('tax_year') for r in rows):warnings.append('Some records have no tax year; the importer cannot distinguish annual versions for those records.')
    return {'record_count': len(rows), 'bpp_record_count': len(personal), 'named_bpp_record_count': named,
            'missing_address_count': sum(not r.get('situs_address_normalized') for r in rows),
            'tax_years': sorted({r['tax_year'] for r in rows if r.get('tax_year')}), 'warnings': warnings}


def file_to_csv(file_bytes: bytes, filename: str) -> str:
    if filename.lower().endswith('.csv'):
        return file_bytes.decode('utf-8-sig')
    if filename.lower().endswith(('.xlsx','.xls')):
        try:
            return excel_bytes_to_csv_text(file_bytes)
        except Exception as exc:
            raise PropertyImportError('Unable to read this Excel export. Save it as CSV or a valid .xlsx file and retry.') from exc
    raise PropertyImportError('Use a CSV or Excel export.')


def preview_property_file(jurisdiction, file_bytes, filename, *, mapping=None):
    csv_text = file_to_csv(file_bytes, filename)
    reader = csv.DictReader(StringIO(csv_text))
    columns = reader.fieldnames or []
    if not columns or len(columns) != len(set(columns)):
        raise PropertyImportError('The export needs a header row with distinct column names.')
    selected = mapping if mapping is not None else suggest_mapping(columns, jurisdiction.property_field_mapping)
    from dataclasses import replace
    preview_jurisdiction = replace(jurisdiction, property_field_mapping=selected)
    try:
        result = import_property_csv(preview_jurisdiction, csv_text, dry_run=True)
        validation_error = None
    except PropertyImportError as exc:
        result = None
        validation_error = str(exc)
    from itertools import islice
    sample_rows=list(islice(csv.DictReader(StringIO(csv_text)),3))
    return {'columns': columns, 'mapping': selected, 'sample_rows':sample_rows, 'fields': list(IMPORT_FIELDS),
            'result': result, 'csv_text': csv_text, 'validation_error': validation_error}
