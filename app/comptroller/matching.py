"""Transparent, rule-based matching of a closed Comptroller permit location to
a RenditionPilot rendition record.

SCHEMA, VERIFIED LIVE 2026-08-21: there is no `accounts` table (or
`renditions`/`rendition_accounts`/etc.) in the production Supabase project --
those names, referenced by supabase/migrations/20260429_multi_district_renditions.sql,
do not exist. `GET {SUPABASE_URL}/rest/v1/` (PostgREST's schema listing) shows
the real tables are pipeline-shaped: `rendition_uploads` -> `rendition_jobs` ->
`parsed_rendition_results`, plus `review_notes`/`manual_overrides`/`exports`.
None of them are a master "account" registry, and NONE of them carry a situs
address, city, ZIP, or DBA name as a column -- confirmed against
`app/pipeline.py`'s `_extract_metadata()` (line ~3030), which is the only code
that ever produces business-identity data from a rendition PDF, and only ever
extracts `owner_name`, `account_number`, `tax_year`, `signed_date`. That dict
is nested under a `"metadata"` key in the pipeline's result (`app/pipeline.py`
lines 3203/3289), which is presumably what lands in `parsed_rendition_results.result`
(jsonb) once persistence is wired up -- see the "NOT YET POPULATED" warning
below.

Given that, RenditionPilot has exactly ONE usable, uncorroborated matching
signal today: business-name similarity against `owner_name`. There is no
address/ZIP to cross-check it against, and `account_number` is a RenditionPilot-
internal appraisal account number in a completely different numbering space
from the Comptroller's `taxpayer_id`/`location_number` -- it cannot be
compared for equality, only carried through as a display field on a match.

Because of that, HIGH confidence is unreachable from name evidence alone: a
single name-similarity signal, with nothing to corroborate it, is exactly the
kind of "maybe, but I can't be sure" evidence that should never be labeled
with the same confidence as a genuinely corroborated match (address + ZIP +
name, in the original address-aware design this module had before the real
schema was inspected).

Property Enrichment (see property_matching.py/property_enrichment.py) adds
that missing second signal back, from an independent source: a Comptroller
permit's address can be resolved against a jurisdiction's real-property
records (loaded from a county property export, not from RenditionPilot's own
account data, which still has no address). `match_closure_to_account`'s
optional `property_match` parameter carries that result in; HIGH is only ever
returned when BOTH a strong name match AND an exact/strong property-address
match agree on the SAME account number -- specifically, the candidate's
BPP `account_number` matching one of the personal-property (P-account)
numbers Property Enrichment found at that address, via
`property_match.personal_property_accounts`.

This is deliberately NOT compared against the property record's own
`real_account_number`. A real Texas CAD property export mixes both account
types under one column: 'R'-prefixed numbers for real property (the land)
and 'P'-prefixed numbers for business personal property, and a single situs
address routinely has one real-property record plus zero or more
personal-property records for whatever businesses operate there (see
property_matching.classify_account_type). A BPP rendition's account number
is always P-style; comparing it against an R-style real-property account
would never match, by definition, no matter how exact the address is --
that would make HIGH permanently unreachable even with a perfect CRS
export. An exact address match alone, a name match alone, or a real-account
match alone is never enough for HIGH. Jurisdictions with no property data
loaded (`property_match=None`, the default) get the exact unreachable-HIGH
behavior described above, unchanged.

NOT YET POPULATED: as of 2026-08-21, `rendition_uploads`, `rendition_jobs`,
and `parsed_rendition_results` all exist but contain zero rows, and nothing
in `backend/main.py` or `app/*.py` currently writes to any of them -- the
deployed review flow (`/api/review/run`, `/api/review/lock`, `/api/review/save`)
returns results to the browser and (in the legacy Streamlit app only) writes
local files under Output/, but never persists to Supabase. This module is
correct against the real schema and will start matching real data the moment
that persistence gap is closed elsewhere in the app; until then, every
closure will be UNMATCHED in production for lack of any candidate rows, which
is the honest, correct behavior (never fabricate a match).

Configuration (all env-var overridable, in case the schema/JSON shape above
turns out to differ once real data exists):

  COMPTROLLER_MATCH_TABLE                (default: parsed_rendition_results)
  COMPTROLLER_MATCH_ID_COLUMN            (default: id)
  COMPTROLLER_MATCH_DISTRICT_ID_COLUMN   (default: district_id)
  COMPTROLLER_MATCH_TAX_YEAR_COLUMN      (default: tax_year)
  COMPTROLLER_MATCH_ACCOUNT_NUMBER_PATH  (default: result->metadata->>account_number)
  COMPTROLLER_MATCH_OWNER_NAME_PATH      (default: result->metadata->>owner_name)

Matching is deliberately simple and inspectable rather than a black box: the
human-readable reason states exactly what did and didn't match, and a result
is flagged `ambiguous` when two or more RenditionPilot records score similarly
well against the same closure (e.g. the same owner name recurring across
multiple rendition years/uploads) -- in that case the single "best" pick is
not treated as confidently identified, since nothing here can distinguish
*which* of several same-named records is the one that actually closed.
"""

from __future__ import annotations

import difflib
import os
import re
from dataclasses import dataclass, field

import requests

from app.comptroller.service import ComptrollerServiceError, get_supabase_config

NAME_STRONG_THRESHOLD = 0.85
NAME_PARTIAL_THRESHOLD = 0.6
AMBIGUITY_SCORE_GAP = 0.05  # candidates within this ratio of the best score are "similarly good"

_BUSINESS_SUFFIX_PATTERN = re.compile(
    r"\b(INC|INCORPORATED|LLC|L L C|LTD|LP|L P|CORP|CORPORATION|CO|COMPANY)\b\.?"
)


class MatchingConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class MatchCandidate:
    record_id: str
    account_number: str | None
    owner_name: str | None
    tax_year: int | None


@dataclass(frozen=True)
class MatchResult:
    confidence: str  # HIGH | MEDIUM | LOW | UNMATCHED (HIGH is currently unreachable -- see module docstring)
    score: float
    reason: str
    candidate: MatchCandidate | None
    ambiguous: bool = False
    # Per-signal breakdown for a transparent UI (e.g. "Address: NOT AVAILABLE,
    # DBA: MATCH, Legal Entity: NO MATCH") -- see build_signal_breakdown().
    # Signals RenditionPilot has no data for are explicitly "NOT AVAILABLE",
    # never silently omitted or reported as a false "NO MATCH".
    signals: dict[str, str] = field(default_factory=dict)
    # True when the DBA/business name matched well but the legal/taxpayer
    # name did not (or vice versa) -- the single-field ownership-change hint
    # described in matching.py's module docstring: RenditionPilot only
    # stores one name per rendition record, so a strong match on one of the
    # Comptroller's two name fields with a poor match on the other can't be
    # fully resolved, only flagged for a human to look at.
    name_signals_diverge: bool = False


def normalize_name(value: str | None) -> str:
    if not value:
        return ""
    text = value.upper()
    # Drop apostrophes without inserting a space, so "JOE'S" -> "JOES" (not
    # "JOE S") -- otherwise a possessive name never matches its own
    # unpunctuated form, e.g. across "Joe's Sports Bar" vs "JOES SPORTS BAR".
    text = text.replace("'", "").replace("’", "")
    text = _BUSINESS_SUFFIX_PATTERN.sub("", text)
    text = re.sub(r"[^A-Z0-9 ]", " ", text)
    return " ".join(text.split())


def _name_similarity(a: str | None, b: str | None) -> float:
    a_norm, b_norm = normalize_name(a), normalize_name(b)
    if not a_norm or not b_norm:
        return 0.0
    return difflib.SequenceMatcher(None, a_norm, b_norm).ratio()


def _match_config() -> dict[str, str]:
    return {
        "table": os.getenv("COMPTROLLER_MATCH_TABLE", "parsed_rendition_results"),
        "id": os.getenv("COMPTROLLER_MATCH_ID_COLUMN", "id"),
        "district_id": os.getenv("COMPTROLLER_MATCH_DISTRICT_ID_COLUMN", "district_id"),
        "tax_year": os.getenv("COMPTROLLER_MATCH_TAX_YEAR_COLUMN", "tax_year"),
        "account_number_path": os.getenv(
            "COMPTROLLER_MATCH_ACCOUNT_NUMBER_PATH", "result->metadata->>account_number"
        ),
        "owner_name_path": os.getenv("COMPTROLLER_MATCH_OWNER_NAME_PATH", "result->metadata->>owner_name"),
    }


def fetch_candidate_records(district_id: str, *, max_rows: int = 20000) -> list[MatchCandidate]:
    """Fetch every rendition record in the given RenditionPilot district as a match candidate.

    Scoped to a single district (Lubbock CAD, in V1) so a closed Lubbock
    permit can never be matched to an unrelated district's record.
    """

    try:
        supabase_url, service_role_key = get_supabase_config()
    except ComptrollerServiceError as exc:
        raise MatchingConfigError(str(exc)) from exc

    cfg = _match_config()
    select = f"record_id:{cfg['id']},account_number:{cfg['account_number_path']},owner_name:{cfg['owner_name_path']},tax_year:{cfg['tax_year']}"
    headers = {
        "apikey": service_role_key,
        "Authorization": f"Bearer {service_role_key}",
    }
    response = requests.get(
        f"{supabase_url}/rest/v1/{cfg['table']}",
        headers=headers,
        params={
            "select": select,
            cfg["district_id"]: f"eq.{district_id}",
            "limit": str(max_rows),
        },
        timeout=20,
    )
    if response.status_code >= 400:
        raise MatchingConfigError(
            f"Fetching candidate rendition records from '{cfg['table']}' failed "
            f"(HTTP {response.status_code}): {response.text[:500]}. Verify the "
            "COMPTROLLER_MATCH_* env vars match the live schema."
        )
    rows = response.json()
    if not isinstance(rows, list):
        raise MatchingConfigError(f"Unexpected response shape from '{cfg['table']}': {rows!r}")

    candidates = []
    for row in rows:
        record_id = row.get("record_id")
        if record_id is None:
            continue
        candidates.append(
            MatchCandidate(
                record_id=str(record_id),
                account_number=row.get("account_number"),
                owner_name=row.get("owner_name"),
                tax_year=row.get("tax_year"),
            )
        )
    return candidates


@dataclass(frozen=True)
class CandidateScore:
    score: float
    reasons: list[str] = field(default_factory=list)
    name_strong: bool = False
    name_partial: bool = False
    dba_score: float = 0.0
    legal_score: float = 0.0


def _signal_label(score: float) -> str:
    if score >= NAME_STRONG_THRESHOLD:
        return "MATCH"
    if score >= NAME_PARTIAL_THRESHOLD:
        return "PARTIAL MATCH"
    if score > 0:
        return "NO MATCH"
    return "NO MATCH"


def score_candidate(
    candidate: MatchCandidate,
    *,
    permit_legal_name: str | None,
    permit_location_name: str | None,
) -> CandidateScore:
    dba_score = _name_similarity(permit_location_name, candidate.owner_name)
    legal_score = _name_similarity(permit_legal_name, candidate.owner_name)
    name_score = max(dba_score, legal_score)

    if name_score >= NAME_STRONG_THRESHOLD:
        return CandidateScore(
            score=name_score, reasons=["strong owner-name match"], name_strong=True,
            dba_score=dba_score, legal_score=legal_score,
        )
    if name_score >= NAME_PARTIAL_THRESHOLD:
        return CandidateScore(
            score=name_score, reasons=["partial owner-name match"], name_partial=True,
            dba_score=dba_score, legal_score=legal_score,
        )
    if name_score > 0:
        return CandidateScore(
            score=name_score, reasons=["owner name differs"], dba_score=dba_score, legal_score=legal_score,
        )
    return CandidateScore(score=0.0, reasons=["no name similarity"], dba_score=dba_score, legal_score=legal_score)


def _detect_name_divergence(cs: CandidateScore) -> bool:
    """One of DBA/legal name matched well while the other didn't -- see
    MatchResult.name_signals_diverge. A candidate that only ever had one
    Comptroller name field to begin with (the other blank) doesn't count."""

    strong = NAME_STRONG_THRESHOLD
    weak = NAME_PARTIAL_THRESHOLD
    one_strong_one_weak = (cs.dba_score >= strong and cs.legal_score < weak) or (
        cs.legal_score >= strong and cs.dba_score < weak
    )
    return one_strong_one_weak


def build_signal_breakdown(
    candidate: MatchCandidate | None,
    cs: CandidateScore | None,
    *,
    property_match: "object | None" = None,
) -> dict[str, str]:
    """Every signal a reviewer might expect to see, explicitly marked
    NOT AVAILABLE where RenditionPilot has no data for it -- never silently
    dropped, so the UI can show the exact table the product spec asks for
    without implying a signal was checked and failed when it was never
    checkable at all.

    `property_match` (a property_matching.PropertyMatchResult, when a
    jurisdiction has real-property data loaded) fills in the address/ZIP/
    suite/property-account rows that would otherwise read NOT AVAILABLE --
    typed loosely here to avoid matching.py depending on property_matching.py
    for anything but this optional display detail.
    """

    breakdown = {
        "address": "NOT AVAILABLE (RenditionPilot has no situs address data)",
        "zip": "NOT AVAILABLE (RenditionPilot has no ZIP data)",
        "suite_unit": "NOT AVAILABLE (RenditionPilot has no address data)",
        "property_account": "NOT AVAILABLE (no CRS/property linkage yet)",
    }
    have_bpp_candidate = candidate is not None and cs is not None
    if have_bpp_candidate:
        breakdown["business_dba_name"] = _signal_label(cs.dba_score)
        breakdown["legal_entity_name"] = _signal_label(cs.legal_score)
        breakdown["existing_rendition_record"] = f"FOUND ({candidate.record_id})"
    else:
        breakdown["business_dba_name"] = "NO MATCH"
        breakdown["legal_entity_name"] = "NO MATCH"
        breakdown["existing_rendition_record"] = "NONE"

    # Property Enrichment's result is independent of whether a BPP name
    # match was found -- deliberately evaluated even when have_bpp_candidate
    # is False, since "no rendition on file to name-match against, but a
    # personal-property account already exists at this address" is exactly
    # the signal a reviewer most needs for a NO_ACCOUNT_FOUND item. This was
    # a real bug: this block used to live inside the `if have_bpp_candidate`
    # branch, so a property match never showed up on any item where name
    # matching had nothing to compare against -- i.e. every item, in
    # production, before real rendition data exists.
    if property_match is not None and getattr(property_match, "signals", None):
        pm_signals = property_match.signals
        breakdown["address"] = f"{property_match.classification} ({pm_signals.get('street_name', 'NO MATCH')})"
        breakdown["zip"] = pm_signals.get("zip", breakdown["zip"])
        breakdown["suite_unit"] = pm_signals.get("suite", breakdown["suite_unit"])
        matched_property = property_match.matched_property
        personal_accounts = getattr(property_match, "personal_property_accounts", None) or []

        # Real (R) and personal-property (P) accounts are different
        # identifier spaces (see property_matching.classify_account_type) --
        # a BPP rendition's account number is always P-style, so it is only
        # ever compared against personal_property_accounts, never against
        # the property's own real_account_number (always R-style when
        # present). Comparing a P-account to an R-account would silently
        # never match, which would make HIGH confidence permanently
        # unreachable even with a perfect address match.
        account_matches_personal = bool(
            have_bpp_candidate
            and candidate.account_number
            and any(_accounts_equal(candidate.account_number, p) for p in personal_accounts)
        )
        real_part = f"R-account: {matched_property.real_account_number}" if matched_property and matched_property.real_account_number else "R-account: NOT AVAILABLE"
        if personal_accounts:
            personal_part = f"P-account(s) on file: {', '.join(personal_accounts)}" + (" (MATCHES this BPP account)" if account_matches_personal else "")
        else:
            personal_part = "P-account(s) on file: NONE FOUND"
        breakdown["property_account"] = f"{real_part}; {personal_part}" if (matched_property or personal_accounts) else "NOT AVAILABLE (no property record matched)"
        # Structured (not just the human-readable property_account string
        # above) so downstream consumers -- the Account Card's exception
        # list, in particular -- don't need to parse prose to find out
        # whether a P-account already exists at this address.
        breakdown["personal_property_accounts"] = ", ".join(personal_accounts) if personal_accounts else "NONE FOUND"
        breakdown["real_property_account"] = matched_property.real_account_number if matched_property and matched_property.real_account_number else "NOT AVAILABLE"

    return breakdown


def _clean_account_number(value: str) -> str:
    return "".join(ch for ch in value.upper() if ch.isalnum())


def _accounts_equal(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return _clean_account_number(a) == _clean_account_number(b)


def _classify_confidence(cs: CandidateScore) -> str:
    """HIGH is not reachable from name evidence alone: a lone,
    uncorroborated name-similarity signal is only ever "maybe the same
    business," never "confirmed." match_closure_to_account() may still
    upgrade MEDIUM to HIGH afterward if independent property corroboration
    agrees -- see this module's docstring and property_matching.py.
    """

    if cs.name_strong:
        return "MEDIUM"
    if cs.name_partial:
        return "LOW"
    return "UNMATCHED"


def match_closure_to_account(
    *,
    district_id: str | None,
    permit_legal_name: str | None,
    permit_location_name: str | None,
    candidates: list[MatchCandidate] | None = None,
    # Accepted and ignored: RenditionPilot has no situs/city/ZIP data to
    # match against (see module docstring). Kept so callers built against the
    # earlier address-aware signature don't need to change their call sites.
    permit_address: str | None = None,
    permit_city: str | None = None,
    permit_zip: str | None = None,
    # Optional independent corroboration from Property Enrichment (see
    # property_matching.PropertyMatchResult). None (the default) means "no
    # property data loaded for this jurisdiction" and reproduces the exact
    # name-only behavior this module always had. See module docstring.
    property_match: "object | None" = None,
) -> MatchResult:
    """Match one closure against a district's rendition records.

    `candidates`, when provided, is used as-is instead of calling
    fetch_candidate_records -- month-end processing has many closure events
    per district in the same batch and pre-fetches/caches the district's
    record list once rather than re-querying Supabase for every event.
    """

    empty_signals = build_signal_breakdown(None, None, property_match=property_match)

    if not district_id:
        return MatchResult(
            confidence="UNMATCHED",
            score=0.0,
            reason="No RenditionPilot district mapping configured for this county; record search skipped.",
            candidate=None,
            signals=empty_signals,
        )

    if candidates is None:
        try:
            candidates = fetch_candidate_records(district_id)
        except MatchingConfigError as exc:
            return MatchResult(confidence="UNMATCHED", score=0.0, reason=str(exc), candidate=None, signals=empty_signals)

    if not candidates:
        return MatchResult(
            confidence="UNMATCHED",
            score=0.0,
            reason="No RenditionPilot rendition records found for this district.",
            candidate=None,
            signals=empty_signals,
        )

    scored = [
        (candidate, score_candidate(candidate, permit_legal_name=permit_legal_name, permit_location_name=permit_location_name))
        for candidate in candidates
    ]
    scored.sort(key=lambda pair: pair[1].score, reverse=True)
    best_candidate, best_score = scored[0]

    confidence = _classify_confidence(best_score)
    if confidence == "UNMATCHED":
        return MatchResult(
            confidence="UNMATCHED",
            score=round(best_score.score, 3),
            reason="No RenditionPilot record met the minimum name-similarity threshold.",
            candidate=None,
            signals=build_signal_breakdown(None, None, property_match=property_match),
        )

    # Ambiguous: another candidate scored close enough to the winner that
    # picking "the best one" isn't the same as being confident it's the
    # right one -- e.g. the same owner name appears on rendition records for
    # more than one tax year/upload, or two distinct businesses happen to
    # share a very similar name. Address data would normally break this tie;
    # RenditionPilot has none, so this can only be surfaced, not resolved.
    runner_up_ties = [
        cand
        for cand, sc in scored[1:]
        if sc.score >= best_score.score - AMBIGUITY_SCORE_GAP and sc.score >= NAME_PARTIAL_THRESHOLD
    ]
    ambiguous = bool(runner_up_ties)

    reason = "; ".join(best_score.reasons)
    if ambiguous:
        reason += f"; ambiguous -- {len(runner_up_ties)} other record(s) scored similarly (no address data to disambiguate)"

    name_signals_diverge = _detect_name_divergence(best_score)
    if name_signals_diverge:
        reason += "; business/DBA name and legal taxpayer name disagree -- possible ownership change, review before treating as a confirmed match"

    # Property corroboration (spec: "Make HIGH confidence reachable through
    # corroboration"). Requires ALL of: unambiguous name match, a strong name
    # signal, an exact/strong property-address match, AND that this
    # candidate's own BPP account number appears among the personal-property
    # (P-account) numbers Property Enrichment found at that address.
    #
    # Deliberately NOT compared against matched_property.real_account_number:
    # that's always the R-account (the land record), a completely different
    # identifier space from a BPP rendition's P-account -- they can never be
    # equal, by definition, no matter how exact the address match is. Using
    # the wrong field here would make HIGH permanently unreachable even with
    # perfect real data. See property_matching.classify_account_type.
    # Address match alone, or name match alone, never produces HIGH.
    if (
        not ambiguous
        and best_score.name_strong
        and property_match is not None
        and getattr(property_match, "classification", None) in ("EXACT_PROPERTY_MATCH", "STRONG_PROPERTY_MATCH")
        and best_candidate.account_number
        and any(
            _accounts_equal(best_candidate.account_number, p)
            for p in (getattr(property_match, "personal_property_accounts", None) or [])
        )
    ):
        confidence = "HIGH"
        reason += "; corroborated by a matching personal-property (BPP) account on file at the same address"

    return MatchResult(
        confidence=confidence,
        score=round(best_score.score, 3),
        reason=reason,
        candidate=best_candidate,
        ambiguous=ambiguous,
        signals=build_signal_breakdown(best_candidate, best_score, property_match=property_match),
        name_signals_diverge=name_signals_diverge,
    )
