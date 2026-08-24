from __future__ import annotations

import pytest

from app.comptroller import property_adapter, property_enrichment
from app.comptroller.jurisdictions import Jurisdiction
from app.comptroller.property_adapter import NormalizedRealProperty
from app.comptroller.property_enrichment import (
    PropertyEnrichmentError,
    lookup_property_by_account_number,
    run_property_enrichment,
    same_property_accounts,
)
from tests.comptroller_fakes import FakeSupabase


@pytest.fixture
def fake_supabase(monkeypatch):
    fake = FakeSupabase()
    monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "fake-service-role-key")
    monkeypatch.setattr(property_adapter, "_request_json", fake.request_json)
    monkeypatch.setattr(property_enrichment, "_request_json", fake.request_json)
    return fake


def make_jurisdiction(**overrides) -> Jurisdiction:
    defaults = dict(
        id="jur-1", district_id="district-1", name="Test CAD", slug="test",
        county_name="Test", state="TX", timezone="America/Chicago", active=True,
        comptroller_county_code="999", comptroller_dataset_id="3kx8-uryv",
        capabilities={"real_property_linkage": True}, cad_field_mapping={},
        property_field_mapping={"source_property_id": "PropertyID", "situs_address": "SitusAddress", "real_account_number": "QuickRefID"},
    )
    defaults.update(overrides)
    return Jurisdiction(**defaults)


def seed_property(fake_supabase, jurisdiction_id, pid, addr, zip_=None, acct=None):
    from app.comptroller.address_normalizer import normalize_address

    fake_supabase.real_property_records[f"{jurisdiction_id}::{pid}"] = {
        "id": f"row-{pid}", "jurisdiction_id": jurisdiction_id, "source_property_id": pid,
        "real_account_number": acct, "situs_address_raw": addr,
        "situs_address_normalized": normalize_address(addr, zip_code=zip_).normalized,
        "situs_city": None, "situs_state": None,
        "situs_zip": zip_, "owner_name": None, "tug": None, "neighborhood": None, "map_id": None,
        "latitude": None, "longitude": None, "source_system": "imported_file",
        "source_import_id": None, "source_updated_at": None,
    }


def test_raises_when_capability_not_enabled(fake_supabase):
    jurisdiction = make_jurisdiction(capabilities={})
    with pytest.raises(PropertyEnrichmentError):
        run_property_enrichment(jurisdiction, subject_type="AD_HOC_LOOKUP", subject_id="x", input_address="100 Main St")


def test_raises_when_required_mapping_missing(fake_supabase):
    jurisdiction = make_jurisdiction(property_field_mapping={"real_account_number": "QuickRefID"})
    with pytest.raises(PropertyEnrichmentError):
        run_property_enrichment(jurisdiction, subject_type="AD_HOC_LOOKUP", subject_id="x", input_address="100 Main St")


def test_finds_exact_match_and_stores_result(fake_supabase):
    jurisdiction = make_jurisdiction()
    seed_property(fake_supabase, "jur-1", "P1", "5807 88TH PL", "79424", "R163313")
    outcome = run_property_enrichment(
        jurisdiction, subject_type="NEW_BUSINESS_CANDIDATE", subject_id="tp1:loc1",
        input_address="5807 88th Pl", input_zip="79424",
    )
    assert outcome.from_cache is False
    assert outcome.result.classification == "EXACT_PROPERTY_MATCH"
    stored = fake_supabase.property_enrichment_results["jur-1::NEW_BUSINESS_CANDIDATE::tp1:loc1"]
    assert stored["real_account_number"] == "R163313"
    assert stored["review_status"] == "NOT_REVIEWED"


def test_address_lookup_never_loads_the_full_property_table(fake_supabase):
    """Regression test for a real production bug: the Property Lookup
    endpoint/CLI used to prefetch adapter.search_properties() (every
    property in the jurisdiction) before every single address lookup.
    Against Lubbock's real 234,059-row table that took over a minute per
    request and made the tool look hung. run_property_enrichment's default
    (candidates=None) must always use the targeted, indexed address search."""

    jurisdiction = make_jurisdiction()
    seed_property(fake_supabase, "jur-1", "P1", "5807 88TH PL", "79424", "R163313")
    run_property_enrichment(
        jurisdiction, subject_type="AD_HOC_LOOKUP", subject_id="x",
        input_address="5807 88th Pl", input_zip="79424",
    )
    real_property_gets = [c for c in fake_supabase.calls if c["url"].endswith("real_property_records") and c["method"] == "GET"]
    assert real_property_gets, "expected at least one targeted GET to real_property_records"
    for call in real_property_gets:
        assert "situs_address_normalized" in call["params"], (
            "address lookup must filter by address (targeted query), never fetch the whole table"
        )


def test_second_run_with_unchanged_input_hits_cache(fake_supabase):
    jurisdiction = make_jurisdiction()
    seed_property(fake_supabase, "jur-1", "P1", "5807 88TH PL", "79424", "R163313")
    run_property_enrichment(jurisdiction, subject_type="NEW_BUSINESS_CANDIDATE", subject_id="tp1:loc1", input_address="5807 88th Pl", input_zip="79424")
    fake_supabase.calls.clear()
    outcome = run_property_enrichment(jurisdiction, subject_type="NEW_BUSINESS_CANDIDATE", subject_id="tp1:loc1", input_address="5807 88th Pl", input_zip="79424")
    assert outcome.from_cache is True
    assert outcome.result.classification == "EXACT_PROPERTY_MATCH"
    # A cache hit still does one by-id property lookup to hydrate the
    # cached result's matched_property, but never re-runs the address
    # search or rewrites property_enrichment_results.
    assert not any(call["method"] == "POST" for call in fake_supabase.calls)
    property_gets = [c for c in fake_supabase.calls if c["url"].endswith("real_property_records")]
    assert all("id" in c["params"] for c in property_gets)
    # Regression: a cache hit must still populate normalized_input -- it
    # was silently dropped (reconstructed as None), which made the CLI's
    # "Normalized: ..." line print blank on every cached lookup.
    assert outcome.result.normalized_input is not None
    assert outcome.result.normalized_input.normalized == "5807 88TH PLACE"


def test_changed_input_address_invalidates_cache(fake_supabase):
    jurisdiction = make_jurisdiction()
    seed_property(fake_supabase, "jur-1", "P1", "5807 88TH PL", "79424", "R163313")
    seed_property(fake_supabase, "jur-1", "P2", "100 Main St", "79401", "R999999")
    run_property_enrichment(jurisdiction, subject_type="NEW_BUSINESS_CANDIDATE", subject_id="tp1:loc1", input_address="5807 88th Pl", input_zip="79424")
    outcome = run_property_enrichment(jurisdiction, subject_type="NEW_BUSINESS_CANDIDATE", subject_id="tp1:loc1", input_address="100 Main St", input_zip="79401")
    assert outcome.from_cache is False
    assert outcome.result.matched_property.property_id == "row-P2"


def test_new_property_import_invalidates_cache(fake_supabase):
    jurisdiction = make_jurisdiction()
    seed_property(fake_supabase, "jur-1", "P1", "5807 88TH PL", "79424", "R163313")
    run_property_enrichment(jurisdiction, subject_type="NEW_BUSINESS_CANDIDATE", subject_id="tp1:loc1", input_address="5807 88th Pl", input_zip="79424")
    # A newer import lands after the first enrichment ran.
    fake_supabase.property_source_imports["imp-1"] = {
        "id": "imp-1", "jurisdiction_id": "jur-1", "imported_at": "2099-01-01T00:00:00Z", "row_count": 1,
    }
    outcome = run_property_enrichment(jurisdiction, subject_type="NEW_BUSINESS_CANDIDATE", subject_id="tp1:loc1", input_address="5807 88th Pl", input_zip="79424")
    assert outcome.from_cache is False


def test_force_refresh_bypasses_cache(fake_supabase):
    jurisdiction = make_jurisdiction()
    seed_property(fake_supabase, "jur-1", "P1", "5807 88TH PL", "79424", "R163313")
    run_property_enrichment(jurisdiction, subject_type="NEW_BUSINESS_CANDIDATE", subject_id="tp1:loc1", input_address="5807 88th Pl", input_zip="79424")
    outcome = run_property_enrichment(jurisdiction, subject_type="NEW_BUSINESS_CANDIDATE", subject_id="tp1:loc1", input_address="5807 88th Pl", input_zip="79424", force_refresh=True)
    assert outcome.from_cache is False


def test_refresh_never_resets_a_reviewers_decision(fake_supabase):
    jurisdiction = make_jurisdiction()
    seed_property(fake_supabase, "jur-1", "P1", "5807 88TH PL", "79424", "R163313")
    run_property_enrichment(jurisdiction, subject_type="NEW_BUSINESS_CANDIDATE", subject_id="tp1:loc1", input_address="5807 88th Pl", input_zip="79424")
    fake_supabase.property_enrichment_results["jur-1::NEW_BUSINESS_CANDIDATE::tp1:loc1"]["review_status"] = "ACCEPTED"
    run_property_enrichment(jurisdiction, subject_type="NEW_BUSINESS_CANDIDATE", subject_id="tp1:loc1", input_address="5807 88th Pl", input_zip="79424", force_refresh=True)
    assert fake_supabase.property_enrichment_results["jur-1::NEW_BUSINESS_CANDIDATE::tp1:loc1"]["review_status"] == "ACCEPTED"


def test_dry_run_writes_nothing(fake_supabase):
    jurisdiction = make_jurisdiction()
    seed_property(fake_supabase, "jur-1", "P1", "5807 88TH PL", "79424", "R163313")
    outcome = run_property_enrichment(jurisdiction, subject_type="AD_HOC_LOOKUP", subject_id="dryrun", input_address="5807 88th Pl", input_zip="79424", dry_run=True)
    assert outcome.result.classification == "EXACT_PROPERTY_MATCH"
    assert fake_supabase.property_enrichment_results == {}


def test_cache_hit_with_no_match_still_populates_normalized_input(fake_supabase):
    jurisdiction = make_jurisdiction()
    run_property_enrichment(jurisdiction, subject_type="AD_HOC_LOOKUP", subject_id="x", input_address="999 Nowhere Rd")
    outcome = run_property_enrichment(jurisdiction, subject_type="AD_HOC_LOOKUP", subject_id="x", input_address="999 Nowhere Rd")
    assert outcome.from_cache is True
    assert outcome.result.classification == "NO_PROPERTY_MATCH"
    assert outcome.result.normalized_input is not None
    assert outcome.result.normalized_input.normalized == "999 NOWHERE ROAD"


def test_jurisdiction_isolation_never_matches_another_jurisdictions_property(fake_supabase):
    jurisdiction_a = make_jurisdiction(id="jur-a")
    seed_property(fake_supabase, "jur-b", "P1", "5807 88TH PL", "79424", "R163313")
    outcome = run_property_enrichment(jurisdiction_a, subject_type="AD_HOC_LOOKUP", subject_id="x", input_address="5807 88th Pl", input_zip="79424")
    assert outcome.result.classification == "NO_PROPERTY_MATCH"


class FakeMatchCandidate:
    def __init__(self, record_id, account_number):
        self.record_id = record_id
        self.account_number = account_number


def test_same_property_accounts_cross_references_by_account_number():
    matched = NormalizedRealProperty(
        property_id="row-1", jurisdiction_id="jur-1", source_property_id="P1", tax_year=None,
        real_account_number="R163313", situs_address_raw=None, situs_address_normalized=None,
        situs_city=None, situs_state=None, situs_zip=None, owner_name=None,
        tug=None, neighborhood=None, map_id=None, latitude=None, longitude=None,
        source_system=None, source_import_id=None, source_updated_at=None,
    )
    from app.comptroller.property_matching import PropertyMatchResult
    property_match = PropertyMatchResult(
        classification="EXACT_PROPERTY_MATCH", confidence="HIGH", score=1.0,
        matched_property=matched, candidate_count=1,
    )
    candidates = [FakeMatchCandidate("acc-1", "r-163313"), FakeMatchCandidate("acc-2", "R999999")]
    result = same_property_accounts(property_match, candidates)
    assert [c.record_id for c in result] == ["acc-1"]


def test_same_property_accounts_empty_when_no_match():
    from app.comptroller.property_matching import PropertyMatchResult
    property_match = PropertyMatchResult(classification="NO_PROPERTY_MATCH", confidence="NONE", score=0.0, matched_property=None, candidate_count=0)
    assert same_property_accounts(property_match, [FakeMatchCandidate("acc-1", "R1")]) == []


# -- lookup_property_by_account_number ----------------------------------------
#
# Exact-key lookup by QuickRefID/R-account, added after real-world use
# surfaced that staff often know the account number and want to search by
# it directly rather than re-typing an address.

def test_lookup_by_account_number_exact_match(fake_supabase):
    jurisdiction = make_jurisdiction()
    seed_property(fake_supabase, "jur-1", "P1", "5807 88TH PL", "79424", "R163313")
    result = lookup_property_by_account_number(jurisdiction, "R163313")
    assert result.classification == "EXACT_PROPERTY_MATCH"
    assert result.confidence == "HIGH"
    assert result.matched_property.real_account_number == "R163313"


def test_lookup_by_account_number_no_match(fake_supabase):
    jurisdiction = make_jurisdiction()
    result = lookup_property_by_account_number(jurisdiction, "R999999")
    assert result.classification == "NO_PROPERTY_MATCH"
    assert result.matched_property is None


def test_lookup_by_account_number_ambiguous_when_shared_by_multiple_properties(fake_supabase):
    jurisdiction = make_jurisdiction()
    seed_property(fake_supabase, "jur-1", "P1", "100 MAIN ST", "79401", "R500000")
    seed_property(fake_supabase, "jur-1", "P2", "200 MAIN ST", "79401", "R500000")
    result = lookup_property_by_account_number(jurisdiction, "R500000")
    assert result.classification == "AMBIGUOUS_PROPERTY_MATCH"
    assert result.candidate_count == 2
    assert {a.property_id for a in result.alternatives} == {"row-P1", "row-P2"}


def test_lookup_by_account_number_never_loads_the_full_property_table(fake_supabase):
    """Regression test for the real production bug this endpoint had:
    account-number lookup must be a single indexed equality query, never
    the full-jurisdiction search_properties() scan address lookups
    (correctly) use as a last resort."""

    jurisdiction = make_jurisdiction()
    seed_property(fake_supabase, "jur-1", "P1", "5807 88TH PL", "79424", "R163313")
    lookup_property_by_account_number(jurisdiction, "R163313")
    real_property_calls = [c for c in fake_supabase.calls if c["url"].endswith("real_property_records")]
    assert len(real_property_calls) == 1
    assert real_property_calls[0]["params"].get("real_account_number") == "eq.R163313"


def test_lookup_by_account_number_raises_when_capability_not_configured(fake_supabase):
    jurisdiction = make_jurisdiction(property_field_mapping={})
    with pytest.raises(PropertyEnrichmentError):
        lookup_property_by_account_number(jurisdiction, "R163313")
