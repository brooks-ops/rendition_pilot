"""Client for the Texas Comptroller sales-tax permit location dataset.

Official source: Texas Open Data Portal (Socrata), dataset "All Permitted
Sales Tax Locations and Local Sales Tax Responsibility",
https://data.texas.gov/Government-and-Taxes/All-Permitted-Sales-Tax-Locations-and-Local-Sales-/3kx8-uryv
(SODA API: https://data.texas.gov/resource/3kx8-uryv.json). No API key is
required for read access; an optional Socrata app token can be supplied via
COMPTROLLER_APP_TOKEN to raise the (generous, IP-based) unauthenticated rate
limit and is sent as the X-App-Token header when present.

This dataset -- not the separate "Active Sales Tax Permit Holders" dataset --
is the correct source for closure detection: "Active Sales Tax Permit
Holders" only lists currently-active permits and would require inferring a
closure from a record's *absence*, which the product spec explicitly forbids
("missing from one fetch is not itself sufficient evidence of closure"). This
dataset instead carries every permit active in the last four years plus an
explicit `out_of_business_date` column, so a closure is a positive signal
(a populated date), not an absence.

The dataset has no separate ACTIVE/INACTIVE status column and no distinct
"permit end date" column from a closure-reason column -- `out_of_business_date`
is the single field that encodes both. It is mapped here to
`current_status` + `permit_end_date`:
  out_of_business_date is null      -> current_status = "ACTIVE",   permit_end_date = None
  out_of_business_date is not null  -> current_status = "INACTIVE", permit_end_date = out_of_business_date
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

import requests

DEFAULT_DATASET_ID = "3kx8-uryv"
DEFAULT_BASE_URL = "https://data.texas.gov/resource"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_PAGE_SIZE = 5000


class ComptrollerClientError(RuntimeError):
    """Raised for any Comptroller fetch failure (timeout, HTTP error, malformed payload)."""


@dataclass(frozen=True)
class PermitRecord:
    taxpayer_id: str
    location_number: str
    legal_name: str | None
    location_name: str | None
    address: str | None
    city: str | None
    state: str | None
    zip: str | None
    county_code: str | None
    permit_start_date: date | None
    permit_end_date: date | None
    current_status: str
    raw: dict[str, Any]

    @property
    def key(self) -> tuple[str, str]:
        return (self.taxpayer_id, self.location_number)


@dataclass(frozen=True)
class ComptrollerFetchResult:
    records: list[PermitRecord]
    skipped_row_count: int
    source_data_date: datetime | None
    dataset_id: str
    county_code: str


def _get_config(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def _parse_socrata_date(value: Any) -> date | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    # Socrata floating_timestamp values look like "2026-03-02T00:00:00.000"
    text = text.split("T", 1)[0]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _parse_last_modified(header_value: str | None) -> datetime | None:
    if not header_value:
        return None
    try:
        from email.utils import parsedate_to_datetime

        parsed = parsedate_to_datetime(header_value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except (TypeError, ValueError):
        return None


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_row(row: dict[str, Any]) -> PermitRecord | None:
    taxpayer_id = _clean_text(row.get("tp_number"))
    location_number = _clean_text(row.get("loc_number"))
    if not taxpayer_id or not location_number:
        return None

    out_of_business_date = _parse_socrata_date(row.get("out_of_business_date"))
    current_status = "INACTIVE" if out_of_business_date is not None else "ACTIVE"

    # The Comptroller dataset splits the street number (address_number, e.g.
    # "3612") from the street name (address_text, e.g. "122ND ST") into two
    # separate fields. Name-only matching (the sales-tax closure monitor)
    # never needed this combined -- see matching.py's module docstring -- but
    # Property Enrichment's street-number signal (property_matching.py) does,
    # so this recombines them into one full street address, matching how the
    # Comptroller's own tp_address field already formats it ("3612 122ND ST").
    address_number = _clean_text(row.get("address_number"))
    address_text = _clean_text(row.get("address_text"))
    combined_address = f"{address_number} {address_text}".strip() if address_number else address_text

    return PermitRecord(
        taxpayer_id=taxpayer_id,
        location_number=location_number,
        legal_name=_clean_text(row.get("tp_name")),
        location_name=_clean_text(row.get("loc_name")),
        address=combined_address,
        city=_clean_text(row.get("loc_city")),
        state=_clean_text(row.get("loc_state")),
        zip=_clean_text(row.get("loc_zip")),
        county_code=_clean_text(row.get("loc_county")),
        permit_start_date=_parse_socrata_date(row.get("permit_date")),
        permit_end_date=out_of_business_date,
        current_status=current_status,
        raw=row,
    )


def fetch_county_permits(
    county_code: str,
    *,
    dataset_id: str | None = None,
    base_url: str | None = None,
    timeout_seconds: float | None = None,
    app_token: str | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> ComptrollerFetchResult:
    """Fetch every permit location currently on file for one Comptroller county code.

    Raises ComptrollerClientError on any timeout, HTTP error, or response that
    doesn't parse as a JSON list -- callers must treat that as "the sync
    failed", never as "zero permits exist now".
    """

    resolved_dataset_id = dataset_id or _get_config("COMPTROLLER_DATASET_ID", DEFAULT_DATASET_ID)
    resolved_base_url = base_url or _get_config("COMPTROLLER_BASE_URL", DEFAULT_BASE_URL)
    resolved_timeout = timeout_seconds or float(
        _get_config("COMPTROLLER_REQUEST_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))
    )
    resolved_app_token = app_token or _get_config("COMPTROLLER_APP_TOKEN")

    url = f"{resolved_base_url.rstrip('/')}/{resolved_dataset_id}.json"
    headers = {"X-App-Token": resolved_app_token} if resolved_app_token else {}

    records_by_key: dict[tuple[str, str], PermitRecord] = {}
    skipped_row_count = 0
    source_data_date: datetime | None = None
    offset = 0

    while True:
        params = {
            "loc_county": county_code,
            "$limit": page_size,
            "$offset": offset,
            "$order": ":id",
        }
        try:
            response = requests.get(url, headers=headers, params=params, timeout=resolved_timeout)
        except requests.RequestException as exc:
            raise ComptrollerClientError(f"Comptroller request failed: {exc}") from exc

        if source_data_date is None:
            source_data_date = _parse_last_modified(response.headers.get("Last-Modified"))

        if response.status_code >= 400:
            raise ComptrollerClientError(
                f"Comptroller request returned HTTP {response.status_code}: {response.text[:500]}"
            )

        try:
            page = response.json()
        except ValueError as exc:
            raise ComptrollerClientError("Comptroller response was not valid JSON.") from exc

        if not isinstance(page, list):
            raise ComptrollerClientError(
                f"Comptroller response had an unexpected shape: expected a list, got {type(page).__name__}."
            )

        for row in page:
            if not isinstance(row, dict):
                skipped_row_count += 1
                continue
            record = _parse_row(row)
            if record is None:
                skipped_row_count += 1
                continue
            # Last write wins for duplicate rows within a single fetch.
            records_by_key[record.key] = record

        if len(page) < page_size:
            break
        offset += page_size

    return ComptrollerFetchResult(
        records=list(records_by_key.values()),
        skipped_row_count=skipped_row_count,
        source_data_date=source_data_date,
        dataset_id=resolved_dataset_id,
        county_code=county_code,
    )
