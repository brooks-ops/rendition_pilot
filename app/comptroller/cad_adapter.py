"""Normalized RenditionPilot account/property model + adapter pattern.

The BPP Intelligence Engine (matching, classification, detection) is meant
to operate on ONE normalized shape regardless of which appraisal district's
data it's looking at. A CadAdapter is the seam: it knows how one jurisdiction's
real data source maps into that shape. Detection/matching code should never
import a jurisdiction-specific table/column name directly -- it asks an
adapter for `NormalizedAccount`/`NormalizedProperty` objects instead.

Today there is exactly one real adapter (`RenditionPilotCadAdapter`), because
every configured jurisdiction's account data currently lives in the same
Supabase project (`parsed_rendition_results`, scoped by `district_id`) --
see app/comptroller/matching.py's module docstring for the full account-data
story (no accounts table, no address data, one name field). That module's
`fetch_candidate_records`/`MatchCandidate` are reused here, not duplicated --
this adapter is a thin normalization layer over them, not a second
integration.

If a future jurisdiction's CAD/BPP data lives somewhere genuinely
differently shaped (a different database, a different table layout), add a
new class implementing the same methods as `RenditionPilotCadAdapter` and
select it in `get_cad_adapter()` -- e.g. keyed off a future
`jurisdictions.cad_adapter_type` column. The detection/matching engine does
not need to change.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.comptroller.jurisdictions import Jurisdiction
from app.comptroller.matching import (
    NAME_PARTIAL_THRESHOLD,
    MatchCandidate,
    MatchingConfigError,
    _name_similarity,
    fetch_candidate_records,
)


@dataclass(frozen=True)
class NormalizedAccount:
    account_id: str
    jurisdiction_id: str
    account_number: str | None
    owner_name: str | None
    business_name: str | None
    dba_name: str | None
    situs_address: str | None
    situs_city: str | None
    situs_state: str | None
    situs_zip: str | None
    mailing_address: str | None
    property_type: str | None
    status: str | None
    tax_year: int | None = None


@dataclass(frozen=True)
class NormalizedProperty:
    property_id: str
    jurisdiction_id: str
    real_account_number: str | None
    situs_address: str | None
    owner_name: str | None
    neighborhood: str | None
    map_id: str | None


class RenditionPilotCadAdapter:
    """The one real adapter today: RenditionPilot's own rendition-record
    data (app.comptroller.matching), reused as-is. Declares exactly which
    normalized fields it can actually populate so
    jurisdictions.validate_capability() can tell a required gap from an
    optional one instead of guessing."""

    AVAILABLE_ACCOUNT_FIELDS: frozenset[str] = frozenset({"owner_name", "account_number"})
    AVAILABLE_PROPERTY_FIELDS: frozenset[str] = frozenset()  # no CRS/property data exists yet

    def get_bpp_accounts(self, jurisdiction: Jurisdiction) -> list[NormalizedAccount]:
        if not jurisdiction.district_id:
            return []
        try:
            candidates = fetch_candidate_records(jurisdiction.district_id)
        except MatchingConfigError:
            return []
        return [self._normalize(jurisdiction, candidate) for candidate in candidates]

    def get_account(self, jurisdiction: Jurisdiction, account_id: str) -> NormalizedAccount | None:
        for account in self.get_bpp_accounts(jurisdiction):
            if account.account_id == account_id:
                return account
        return None

    def find_accounts_by_address(self, jurisdiction: Jurisdiction, address: str) -> list[NormalizedAccount]:
        # Documented limitation (not a bug): RenditionPilot has no situs
        # address data anywhere (see matching.py). Returns [] rather than
        # raising, so callers degrade gracefully instead of crashing.
        return []

    def find_accounts_by_name(self, jurisdiction: Jurisdiction, name: str) -> list[NormalizedAccount]:
        return [
            account
            for account in self.get_bpp_accounts(jurisdiction)
            if _name_similarity(name, account.owner_name) >= NAME_PARTIAL_THRESHOLD
        ]

    def get_real_property(self, jurisdiction: Jurisdiction, property_id: str) -> NormalizedProperty | None:
        # Documented limitation: no CRS/real-property data exists in
        # RenditionPilot yet -- see docs/bpp_intelligence_queue.md.
        return None

    def find_property_by_situs(self, jurisdiction: Jurisdiction, address: str) -> list[NormalizedProperty]:
        return []

    @staticmethod
    def _normalize(jurisdiction: Jurisdiction, candidate: MatchCandidate) -> NormalizedAccount:
        return NormalizedAccount(
            account_id=candidate.record_id,
            jurisdiction_id=jurisdiction.id,
            account_number=candidate.account_number,
            owner_name=candidate.owner_name,
            business_name=None,
            dba_name=None,
            situs_address=None,
            situs_city=None,
            situs_state=None,
            situs_zip=None,
            mailing_address=None,
            property_type=None,
            status=None,
            tax_year=candidate.tax_year,
        )


def get_cad_adapter(jurisdiction: Jurisdiction) -> RenditionPilotCadAdapter:
    """Single-adapter factory for now. The natural extension point once a
    second, differently-shaped CAD data source exists is to key this off a
    `jurisdiction.cad_adapter_type` column instead of always returning the
    same class."""

    return RenditionPilotCadAdapter()
