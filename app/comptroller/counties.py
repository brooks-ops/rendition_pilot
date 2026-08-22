"""Texas county name/code resolution and monitored-county configuration.

The Texas Comptroller's open-data feed identifies a permit location's county
with `loc_county`, a text field holding Texas's statewide county number (the
same numbering used in local sales tax jurisdiction codes, e.g. the City of
Lubbock's jurisdiction code is "152-104-03" -> county 152). This is the
correct geographic identifier to filter on -- it is the *location's* county,
not a city-name guess, and it is populated independently of how the
taxpayer's mailing address is formatted.

`TEXAS_COUNTY_CODES` intentionally starts with only the counties this
deployment actually monitors, verified against live data
(https://data.texas.gov/resource/3kx8-uryv.json) rather than hand-transcribed
from an alphabetical list, which is the more failure-prone approach for a
value that silently mis-routes data if wrong. To onboard a new county:

1. Confirm its number, e.g. by querying the dataset for a city known to sit
   entirely inside that county and checking which `loc_county` code
   dominates:
     https://data.texas.gov/resource/3kx8-uryv.json?loc_city=<CITY>&$select=loc_county,count(*)&$group=loc_county
   or cross-reference the Comptroller's published local sales tax
   jurisdiction codes (https://comptroller.texas.gov/taxes/sales/county.php),
   whose county prefix is the same number.
2. Add `"CountyName": "code"` below.
3. Add the county name to the COMPTROLLER_MONITORED_COUNTIES env var.

No code changes beyond step 2 are required to monitor an additional county.
"""

from __future__ import annotations

import os

TEXAS_COUNTY_CODES: dict[str, str] = {
    # Verified 2026-08-18 against https://data.texas.gov/resource/3kx8-uryv.json:
    # of 14,013 rows with loc_city=LUBBOCK, 13,958 (99.6%) carry loc_county=152.
    "Lubbock": "152",
}


def normalize_county_name(name: str) -> str:
    return " ".join(str(name or "").strip().split()).title()


def get_county_code(county_name: str) -> str | None:
    return TEXAS_COUNTY_CODES.get(normalize_county_name(county_name))


def get_monitored_counties() -> list[str]:
    """Return the list of county names this deployment should sync, in order.

    Configured via COMPTROLLER_MONITORED_COUNTIES, a comma-separated list of
    county names (default: "Lubbock"). Unknown county names (no entry in
    TEXAS_COUNTY_CODES) are dropped with a clear error rather than silently
    ignored, since silently skipping a misspelled county would look like a
    healthy sync that simply found nothing.
    """

    raw = os.getenv("COMPTROLLER_MONITORED_COUNTIES", "Lubbock")
    names = [normalize_county_name(part) for part in raw.split(",") if part.strip()]
    unknown = [name for name in names if name not in TEXAS_COUNTY_CODES]
    if unknown:
        raise ValueError(
            "COMPTROLLER_MONITORED_COUNTIES includes counties with no known "
            f"Comptroller county code: {unknown}. Add them to "
            "TEXAS_COUNTY_CODES in app/comptroller/counties.py first."
        )
    return names


def get_district_slug_for_county(county_name: str) -> str | None:
    """Map a monitored county to a RenditionPilot districts.slug, if configured.

    Defaults to RenditionPilot's naming convention (`<county>-cad`) so Lubbock
    resolves to the already-seeded 'lubbock-cad' district. Override per-county
    with COMPTROLLER_DISTRICT_SLUG__<COUNTY_UPPER>, e.g.
    COMPTROLLER_DISTRICT_SLUG__LUBBOCK=lubbock-cad.
    """

    normalized = normalize_county_name(county_name)
    env_key = f"COMPTROLLER_DISTRICT_SLUG__{normalized.upper().replace(' ', '_')}"
    override = os.getenv(env_key)
    if override:
        return override.strip()
    return f"{normalized.lower().replace(' ', '-')}-cad"
