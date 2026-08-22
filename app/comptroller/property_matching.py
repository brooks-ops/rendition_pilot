"""Address-to-property matching: given an input address, which real-property
record (if any) is it most likely the same location as?

Mirrors app/comptroller/matching.py's shape (score -> classify -> explain)
deliberately, but operates on NormalizedRealProperty candidates instead of
BPP accounts, and on street/ZIP/suite signals instead of name signals --
these two matchers answer different questions and are kept independent.
match_closure_to_account() calls into this module's result only as
additional, optional corroborating evidence (see its `property_match`
parameter); it never replaces name matching.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field

from app.comptroller.address_normalizer import NormalizedAddress, normalize_address
from app.comptroller.property_adapter import NormalizedRealProperty

STREET_MATCH_THRESHOLD = 0.90
STREET_PARTIAL_THRESHOLD = 0.7
AMBIGUITY_SCORE_GAP = 0.05


def normalize_account_number(value: str | None) -> str | None:
    """Loose equality for comparing a CRS real-property account number
    against a RenditionPilot MatchCandidate.account_number (the account
    number printed on the rendition itself) -- strips whitespace/punctuation
    and case so 'r-163313' and 'R163313' compare equal."""

    if not value:
        return None
    cleaned = "".join(ch for ch in value.upper() if ch.isalnum())
    return cleaned or None


@dataclass(frozen=True)
class PropertyCandidateScore:
    score: float
    street_number_match: bool
    street_name_signal: str  # MATCH | NORMALIZED MATCH | PARTIAL MATCH | NO MATCH
    zip_signal: str  # MATCH | NO MATCH | NOT AVAILABLE
    suite_signal: str  # EXACT MATCH | MISSING | CONFLICT | NOT APPLICABLE
    suite_conflict: bool


@dataclass(frozen=True)
class PropertyMatchResult:
    classification: str  # EXACT_PROPERTY_MATCH | STRONG_PROPERTY_MATCH | POSSIBLE_PROPERTY_MATCH | AMBIGUOUS_PROPERTY_MATCH | NO_PROPERTY_MATCH
    confidence: str  # HIGH | MEDIUM | LOW | NONE
    score: float
    matched_property: NormalizedRealProperty | None
    candidate_count: int
    reasons: list[str] = field(default_factory=list)
    signals: dict[str, str] = field(default_factory=dict)
    normalized_input: NormalizedAddress | None = None
    alternatives: list[NormalizedRealProperty] = field(default_factory=list)


def _split_street_number(base_address: str) -> tuple[str | None, str]:
    parts = base_address.split(" ", 1)
    if parts and parts[0].isdigit():
        return parts[0], parts[1] if len(parts) > 1 else ""
    return None, base_address


def score_property_candidate(
    input_addr: NormalizedAddress, candidate: NormalizedRealProperty
) -> PropertyCandidateScore:
    candidate_addr = normalize_address(candidate.situs_address_raw, zip_code=candidate.situs_zip)

    input_number, input_street = _split_street_number(input_addr.base_address)
    cand_number, cand_street = _split_street_number(candidate_addr.base_address)
    street_number_match = bool(input_number) and input_number == cand_number

    if input_street == cand_street:
        street_name_signal = "MATCH"
        street_score = 1.0
    else:
        ratio = difflib.SequenceMatcher(None, input_street, cand_street).ratio() if input_street and cand_street else 0.0
        street_score = ratio
        if ratio >= STREET_MATCH_THRESHOLD:
            street_name_signal = "NORMALIZED MATCH"
        elif ratio >= STREET_PARTIAL_THRESHOLD:
            street_name_signal = "PARTIAL MATCH"
        else:
            street_name_signal = "NO MATCH"

    if input_addr.zip5 and candidate_addr.zip5:
        zip_signal = "MATCH" if input_addr.zip5 == candidate_addr.zip5 else "NO MATCH"
    else:
        zip_signal = "NOT AVAILABLE"

    suite_conflict = False
    if not input_addr.has_unit and not candidate_addr.has_unit:
        suite_signal = "NOT APPLICABLE"
    elif input_addr.has_unit and candidate_addr.has_unit:
        if input_addr.unit == candidate_addr.unit:
            suite_signal = "EXACT MATCH"
        else:
            suite_signal = "CONFLICT"
            suite_conflict = True
    else:
        # One side names a unit, the other doesn't -- e.g. the Comptroller
        # record has no suite but the property record does (or vice versa).
        # Not proof of a mismatch (a single-tenant building has no suite to
        # report) but not full corroboration either.
        suite_signal = "MISSING"

    score = 0.0
    if street_number_match:
        score += 0.45
    score += 0.35 * street_score
    if zip_signal == "MATCH":
        score += 0.2
    elif zip_signal == "NO MATCH":
        score -= 0.2
    if suite_conflict:
        score -= 0.35  # a suite conflict materially reduces confidence (spec item 9)
    score = max(0.0, min(1.0, score))

    return PropertyCandidateScore(
        score=score,
        street_number_match=street_number_match,
        street_name_signal=street_name_signal,
        zip_signal=zip_signal,
        suite_signal=suite_signal,
        suite_conflict=suite_conflict,
    )


def _classify(cs: PropertyCandidateScore) -> tuple[str, str]:
    """(classification, confidence) for a single best-scoring candidate.
    A suite conflict caps the result at POSSIBLE even with an otherwise
    perfect address match -- two different tenants at the same building are
    two different accounts, not corroborating evidence for either."""

    exact_address = cs.street_number_match and cs.street_name_signal == "MATCH"
    strong_address = cs.street_number_match and cs.street_name_signal in ("MATCH", "NORMALIZED MATCH")

    if cs.suite_conflict:
        return "POSSIBLE_PROPERTY_MATCH", "LOW"

    if exact_address and cs.zip_signal in ("MATCH", "NOT AVAILABLE") and cs.suite_signal in ("EXACT MATCH", "NOT APPLICABLE"):
        return "EXACT_PROPERTY_MATCH", "HIGH"
    if strong_address and cs.zip_signal != "NO MATCH":
        return "STRONG_PROPERTY_MATCH", "HIGH" if cs.zip_signal == "MATCH" else "MEDIUM"
    if cs.street_name_signal in ("MATCH", "NORMALIZED MATCH", "PARTIAL MATCH") and cs.score >= STREET_PARTIAL_THRESHOLD - 0.15:
        return "POSSIBLE_PROPERTY_MATCH", "MEDIUM" if cs.score >= 0.5 else "LOW"
    return "NO_PROPERTY_MATCH", "NONE"


def build_property_signal_breakdown(cs: PropertyCandidateScore | None) -> dict[str, str]:
    if cs is None:
        return {
            "street_number": "NO MATCH",
            "street_name": "NO MATCH",
            "zip": "NOT AVAILABLE",
            "suite": "NOT APPLICABLE",
        }
    return {
        "street_number": "MATCH" if cs.street_number_match else "NO MATCH",
        "street_name": cs.street_name_signal,
        "zip": cs.zip_signal,
        "suite": cs.suite_signal,
    }


def match_property(
    input_address: str | None,
    *,
    input_zip: str | None = None,
    candidates: list[NormalizedRealProperty],
) -> PropertyMatchResult:
    """Match one address against a jurisdiction's real-property candidates.
    Never auto-picks a winner when evidence is ambiguous (spec item 11) --
    returns AMBIGUOUS_PROPERTY_MATCH with every close candidate surfaced
    instead."""

    normalized_input = normalize_address(input_address, zip_code=input_zip)

    if not normalized_input.normalized:
        return PropertyMatchResult(
            classification="NO_PROPERTY_MATCH", confidence="NONE", score=0.0,
            matched_property=None, candidate_count=0,
            reasons=["No input address to match against."],
            signals=build_property_signal_breakdown(None),
            normalized_input=normalized_input,
        )

    if not candidates:
        return PropertyMatchResult(
            classification="NO_PROPERTY_MATCH", confidence="NONE", score=0.0,
            matched_property=None, candidate_count=0,
            reasons=["No real-property records available for this jurisdiction."],
            signals=build_property_signal_breakdown(None),
            normalized_input=normalized_input,
        )

    scored = [(cand, score_property_candidate(normalized_input, cand)) for cand in candidates]
    scored.sort(key=lambda pair: pair[1].score, reverse=True)
    best_candidate, best_score = scored[0]

    classification, confidence = _classify(best_score)

    if classification == "NO_PROPERTY_MATCH":
        return PropertyMatchResult(
            classification="NO_PROPERTY_MATCH", confidence="NONE", score=round(best_score.score, 3),
            matched_property=None, candidate_count=len(candidates),
            reasons=["No real-property record matched closely enough."],
            signals=build_property_signal_breakdown(None),
            normalized_input=normalized_input,
        )

    runner_up_ties = [
        (cand, sc) for cand, sc in scored[1:]
        if sc.score >= best_score.score - AMBIGUITY_SCORE_GAP and sc.street_name_signal in ("MATCH", "NORMALIZED MATCH", "PARTIAL MATCH")
    ]

    if runner_up_ties:
        alternatives = [best_candidate] + [cand for cand, _ in runner_up_ties]
        return PropertyMatchResult(
            classification="AMBIGUOUS_PROPERTY_MATCH", confidence="LOW", score=round(best_score.score, 3),
            matched_property=None, candidate_count=len(candidates),
            reasons=[f"{len(runner_up_ties) + 1} property records scored similarly for this address."],
            signals=build_property_signal_breakdown(best_score),
            normalized_input=normalized_input,
            alternatives=alternatives,
        )

    reasons = [f"street number {'matched' if best_score.street_number_match else 'did not match'}", f"street name {best_score.street_name_signal.lower()}"]
    if best_score.suite_conflict:
        reasons.append("suite/unit conflict reduced confidence")

    return PropertyMatchResult(
        classification=classification, confidence=confidence, score=round(best_score.score, 3),
        matched_property=best_candidate, candidate_count=len(candidates),
        reasons=reasons,
        signals=build_property_signal_breakdown(best_score),
        normalized_input=normalized_input,
    )
