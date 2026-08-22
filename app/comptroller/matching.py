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

Because of that, HIGH confidence is intentionally unreachable by this
module's logic: a single name-similarity signal, with nothing to corroborate
it, is exactly the kind of "maybe, but I can't be sure" evidence that should
never be labeled with the same confidence as a genuinely corroborated match
(address + ZIP + name, in the original address-aware design this module had
before the real schema was inspected). If RenditionPilot's data model is ever
extended to store a situs address, city/ZIP, or a cross-referenced taxpayer
ID, add that as a second signal here and HIGH becomes reachable again -- see
git history / docs/comptroller_closure_monitor.md for the address-aware
version this replaced.

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


def score_candidate(
    candidate: MatchCandidate,
    *,
    permit_legal_name: str | None,
    permit_location_name: str | None,
) -> CandidateScore:
    name_score = max(
        _name_similarity(permit_location_name, candidate.owner_name),
        _name_similarity(permit_legal_name, candidate.owner_name),
    )

    if name_score >= NAME_STRONG_THRESHOLD:
        return CandidateScore(score=name_score, reasons=["strong owner-name match"], name_strong=True)
    if name_score >= NAME_PARTIAL_THRESHOLD:
        return CandidateScore(score=name_score, reasons=["partial owner-name match"], name_partial=True)
    if name_score > 0:
        return CandidateScore(score=name_score, reasons=["owner name differs"])
    return CandidateScore(score=0.0, reasons=["no name similarity"])


def _classify_confidence(cs: CandidateScore) -> str:
    """HIGH is not reachable here: a lone, uncorroborated name-similarity
    signal (no address/ZIP/cross-referenced ID exists in RenditionPilot's
    current data model to check it against) is only ever "maybe the same
    business," never "confirmed." See the module docstring for what would
    need to change for HIGH to become reachable.
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
) -> MatchResult:
    """Match one closure against a district's rendition records.

    `candidates`, when provided, is used as-is instead of calling
    fetch_candidate_records -- month-end processing has many closure events
    per district in the same batch and pre-fetches/caches the district's
    record list once rather than re-querying Supabase for every event.
    """

    if not district_id:
        return MatchResult(
            confidence="UNMATCHED",
            score=0.0,
            reason="No RenditionPilot district mapping configured for this county; record search skipped.",
            candidate=None,
        )

    if candidates is None:
        try:
            candidates = fetch_candidate_records(district_id)
        except MatchingConfigError as exc:
            return MatchResult(confidence="UNMATCHED", score=0.0, reason=str(exc), candidate=None)

    if not candidates:
        return MatchResult(
            confidence="UNMATCHED",
            score=0.0,
            reason="No RenditionPilot rendition records found for this district.",
            candidate=None,
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

    return MatchResult(
        confidence=confidence,
        score=round(best_score.score, 3),
        reason=reason,
        candidate=best_candidate,
        ambiguous=ambiguous,
    )
