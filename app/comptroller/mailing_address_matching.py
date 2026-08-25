"""Mailing-address comparison: given a current known mailing address and a
new observation, is this a material change, a formatting-only difference,
or something staff should review? Mirrors property_matching.py's shape
(normalize -> score components -> classify -> explain) deliberately -- a
third independent matcher in this codebase, answering yet another
different question (has correspondence changed?) from name matching
(matching.py) and situs-property matching (property_matching.py).

CORE PRINCIPLE (spec): never generate an alert merely because two address
strings are formatted differently -- comparison is component-level
(address type, street/PO-box line, unit, city, state, zip) so the
explanation names exactly what changed, never just a similarity score.

Deliberately produces only a CHANGE classification/confidence here --
whether an observation is confidently tied to the right account (IDENTITY
confidence) is a separate question this module knows nothing about, per
spec item 29 ("do not collapse these conceptually"); see
mailing_address_intelligence.py for where the two are combined.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.comptroller.address_normalizer import NormalizedMailingAddress, normalize_mailing_address

SAME_ADDRESS = "SAME_ADDRESS"
FORMAT_ONLY_DIFFERENCE = "FORMAT_ONLY_DIFFERENCE"
POSSIBLE_CHANGE = "POSSIBLE_CHANGE"
LIKELY_CHANGE = "LIKELY_CHANGE"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


@dataclass(frozen=True)
class MailingAddressComparison:
    classification: str
    change_confidence: str  # HIGH | MEDIUM | LOW | NONE
    current: NormalizedMailingAddress | None
    observed: NormalizedMailingAddress | None
    differences: dict[str, str] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)


def _diff(current: object | None, observed: object | None) -> str:
    if current is None or observed is None:
        return "NOT AVAILABLE"
    return "SAME" if current == observed else "CHANGED"


def _unit_diff(current_unit: str | None, observed_unit: str | None) -> str:
    # Unlike other components, a missing unit is a real, comparable value
    # ("no suite at this address"), not unknown data -- None vs. None is
    # NOT APPLICABLE, but None vs. a real unit is a genuine addition/removal
    # (CHANGED), never "NOT AVAILABLE" (which would make a suite addition
    # silently invisible to the classifier below).
    if current_unit is None and observed_unit is None:
        return "NOT APPLICABLE"
    return "SAME" if current_unit == observed_unit else "CHANGED"


def _build_differences(current: NormalizedMailingAddress, observed: NormalizedMailingAddress) -> dict[str, str]:
    return {
        "address_type": _diff(current.address_type, observed.address_type),
        "line": _diff(current.normalized_line or None, observed.normalized_line or None),
        "unit": _unit_diff(current.unit, observed.unit),
        "city": _diff(current.city, observed.city),
        "state": _diff(current.state, observed.state),
        "zip": _diff(current.zip5, observed.zip5),
        "zip4": _diff(current.zip4, observed.zip4),
    }


def compare_mailing_addresses(
    *,
    current_raw: str | None,
    current_city: str | None = None,
    current_state: str | None = None,
    current_zip: str | None = None,
    observed_raw: str | None,
    observed_city: str | None = None,
    observed_state: str | None = None,
    observed_zip: str | None = None,
) -> MailingAddressComparison:
    """Compares one "current known" mailing address against one new
    observation. Never invents a change from a blank input on either
    side (spec item 30) -- both directions of missing data are
    INSUFFICIENT_DATA, not a change.
    """

    # A source column can be blank even when a raw line exists (a plain
    # street address with no city on file, say) -- only treat the address
    # as genuinely absent when there's no usable text anywhere.
    current_present = bool((current_raw or "").strip() or (current_city or "").strip())
    observed_present = bool((observed_raw or "").strip() or (observed_city or "").strip())

    if not observed_present:
        return MailingAddressComparison(
            classification=INSUFFICIENT_DATA, change_confidence="NONE", current=None, observed=None,
            reasons=["No new address observation to compare -- a blank observation is never interpreted as a change."],
        )
    if not current_present:
        return MailingAddressComparison(
            classification=INSUFFICIENT_DATA, change_confidence="NONE", current=None, observed=None,
            reasons=["No current mailing address on file to compare against."],
        )

    current = normalize_mailing_address(current_raw, city=current_city, state=current_state, zip_code=current_zip)
    observed = normalize_mailing_address(observed_raw, city=observed_city, state=observed_state, zip_code=observed_zip)

    if current.full_normalized == observed.full_normalized:
        return MailingAddressComparison(
            classification=SAME_ADDRESS, change_confidence="NONE", current=current, observed=observed,
            differences=_build_differences(current, observed),
            reasons=["Normalized addresses are identical."],
        )

    differences = _build_differences(current, observed)

    # Material signals: address type (street vs PO box), the line itself,
    # city, or state actually differing. ZIP is intentionally excluded from
    # this list on its own -- see the zip4-only-difference case below and
    # spec item 9's "79401 -> 79401-1234 is not material" example; a
    # genuine ZIP5 change is still material, checked separately.
    material_flags = [
        differences["address_type"] == "CHANGED",
        differences["line"] == "CHANGED",
        differences["city"] == "CHANGED",
        differences["state"] == "CHANGED",
        current.zip5 is not None and observed.zip5 is not None and current.zip5 != observed.zip5,
    ]

    if any(material_flags):
        reasons = [f"{key.replace('_', ' ')}: {value}" for key, value in differences.items() if value == "CHANGED"]
        return MailingAddressComparison(
            classification=LIKELY_CHANGE, change_confidence="HIGH", current=current, observed=observed,
            differences=differences, reasons=reasons or ["Material address components differ."],
        )

    # Nothing material differs. A suite/unit was added where neither had
    # one before, or a genuinely different unit is now present, without any
    # other change -- spec item 9 example ("STE 100 -> STE 500") calls a
    # suite CHANGE material, but a suite ADDED where the base address is
    # otherwise identical is hedged ("may be meaningful depending on
    # context") -- treated as POSSIBLE, not LIKELY, reflecting that
    # uncertainty honestly rather than picking one side of it.
    if differences["unit"] == "CHANGED":
        if current.has_unit and observed.has_unit:
            return MailingAddressComparison(
                classification=LIKELY_CHANGE, change_confidence="MEDIUM", current=current, observed=observed,
                differences=differences, reasons=[f"Suite/unit changed: {current.unit} -> {observed.unit}"],
            )
        return MailingAddressComparison(
            classification=POSSIBLE_CHANGE, change_confidence="LOW", current=current, observed=observed,
            differences=differences,
            reasons=["Suite/unit added or removed with no other change -- may or may not be meaningful."],
        )

    # Only a ZIP+4 addition/removal/change, or pure formatting (suffix
    # abbreviation, punctuation, PO Box variant spelling) -- not material.
    return MailingAddressComparison(
        classification=FORMAT_ONLY_DIFFERENCE, change_confidence="NONE", current=current, observed=observed,
        differences=differences,
        reasons=["Only formatting or a ZIP+4-only difference -- not a material address change."],
    )
