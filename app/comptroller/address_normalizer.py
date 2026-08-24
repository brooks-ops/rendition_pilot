"""Shared address normalization for Property Enrichment.

Used to compare a Comptroller/BPP address string against a real-property
record's situs address regardless of how each source abbreviates street
types, directions, and unit markers. This is the one normalizer for that
job -- new_business.py has no address normalizer of its own to extend (it
only ever compared owner names, see matching.py's module docstring), so
this is new, shared infrastructure rather than a second implementation of
something that already existed.

Never discards the original string: `NormalizedAddress.raw` preserves
exactly what was passed in, `normalized` is what matching compares against.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_STREET_SUFFIX_MAP: dict[str, str] = {
    "ST": "STREET", "STR": "STREET",
    "RD": "ROAD",
    "AVE": "AVENUE", "AV": "AVENUE",
    "BLVD": "BOULEVARD", "BLV": "BOULEVARD",
    "DR": "DRIVE",
    "LN": "LANE",
    "HWY": "HIGHWAY",
    "FM": "FARM TO MARKET ROAD",
    "RM": "RANCH TO MARKET ROAD",
    "CT": "COURT",
    "CIR": "CIRCLE",
    "PL": "PLACE",
    "PLZ": "PLAZA",
    "PKWY": "PARKWAY", "PKY": "PARKWAY",
    "TRL": "TRAIL", "TR": "TRAIL",
    "TER": "TERRACE",
    "LOOP": "LOOP",
    "WAY": "WAY",
    "SQ": "SQUARE",
    "XING": "CROSSING",
}

_DIRECTION_MAP: dict[str, str] = {
    "N": "NORTH", "S": "SOUTH", "E": "EAST", "W": "WEST",
    "NE": "NORTHEAST", "NW": "NORTHWEST", "SE": "SOUTHEAST", "SW": "SOUTHWEST",
}

_UNIT_MARKERS = {"STE", "SUITE", "UNIT", "APT", "BLDG", "BUILDING", "RM", "ROOM", "FL", "FLOOR", "LOT"}

_UNIT_PATTERN = re.compile(
    r"[,\s]+(?:#\s*|\b(?:" + "|".join(_UNIT_MARKERS) + r")\b\.?\s*)([A-Z0-9-]+)\s*$"
)


@dataclass(frozen=True)
class NormalizedAddress:
    raw: str | None
    normalized: str
    base_address: str  # normalized, with unit info stripped
    unit: str | None
    zip5: str | None
    zip4: str | None

    @property
    def has_unit(self) -> bool:
        return self.unit is not None


def _normalize_zip(value: str | None) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    digits = re.sub(r"[^0-9]", "", value)
    if len(digits) >= 9:
        return digits[:5], digits[5:9]
    if len(digits) >= 5:
        return digits[:5], None
    return (digits or None), None


def _expand_tokens(text: str) -> str:
    tokens = text.split()
    expanded = []
    for token in tokens:
        clean = token.rstrip(".")
        if clean in _STREET_SUFFIX_MAP:
            expanded.append(_STREET_SUFFIX_MAP[clean])
        elif clean in _DIRECTION_MAP:
            expanded.append(_DIRECTION_MAP[clean])
        else:
            expanded.append(clean)
    return " ".join(expanded)


def extract_unit(value: str) -> tuple[str, str | None]:
    """Split a normalized (uppercase, punctuation-stripped) address into
    (base_address, unit) -- e.g. "123 MAIN ST STE 200" -> ("123 MAIN ST", "200").
    Returns (value, None) when no unit marker is present."""

    match = _UNIT_PATTERN.search(value)
    if not match:
        return value, None
    unit = match.group(1)
    base = value[: match.start()].strip()
    return base, unit


def normalize_address(raw: str | None, *, zip_code: str | None = None) -> NormalizedAddress:
    """Normalize a raw address string for matching. `zip_code`, when given
    separately (e.g. a Comptroller record's own zip column), is combined
    with any ZIP embedded in `raw`; the explicit column wins if both exist."""

    if not raw or not raw.strip():
        zip5, zip4 = _normalize_zip(zip_code)
        return NormalizedAddress(raw=raw, normalized="", base_address="", unit=None, zip5=zip5, zip4=zip4)

    text = raw.upper()
    text = text.replace("#", " # ")

    # Detect a trailing ZIP (with optional +4) before punctuation stripping
    # destroys the hyphen that separates the two halves.
    stripped = text.strip().rstrip(".,")
    embedded_zip_match = re.search(r"(\d{5})(?:-(\d{4}))?\s*$", stripped)
    embedded_zip5 = embedded_zip4 = None
    if embedded_zip_match:
        embedded_zip5, embedded_zip4 = embedded_zip_match.group(1), embedded_zip_match.group(2)
        text = stripped[: embedded_zip_match.start()]

    # Real CAD situs exports commonly give "STREET, CITY" or
    # "STREET, CITY, STATE" as one field (e.g. Lubbock's SitusAddress:
    # "5807 88TH PL, LUBBOCK, TX") rather than a clean street-only line.
    # Keep only the portion before the first comma for matching -- without
    # this, a real property's own city name (e.g. "LUBBOCK") gets treated
    # as trailing street text and an otherwise-exact match scores as no
    # match at all. Found importing the real 234k-row Lubbock export: the
    # known 5807 88TH PL -> PropertyID 813538 example failed to match until
    # this was added.
    text = text.split(",")[0]

    text = re.sub(r"[.,]", " ", text)
    text = re.sub(r"[^A-Z0-9# ]", " ", text)
    text = " ".join(text.split())

    text = _expand_tokens(text)
    base, unit = extract_unit(text)

    explicit_zip5, explicit_zip4 = _normalize_zip(zip_code)
    zip5 = explicit_zip5 or embedded_zip5
    zip4 = explicit_zip4 or embedded_zip4

    return NormalizedAddress(raw=raw, normalized=text, base_address=base, unit=unit, zip5=zip5, zip4=zip4)
