from __future__ import annotations

from app.comptroller import cad_adapter
from app.comptroller.jurisdictions import Jurisdiction
from app.comptroller.matching import MatchCandidate, MatchingConfigError


def make_jurisdiction(**overrides) -> Jurisdiction:
    defaults = dict(
        id="jur-lubbock",
        district_id="district-lubbock",
        name="Lubbock Central Appraisal District",
        slug="lubbock",
        county_name="Lubbock",
        state="TX",
        timezone="America/Chicago",
        active=True,
        comptroller_county_code="152",
        comptroller_dataset_id="3kx8-uryv",
        capabilities={"new_business_detection": True},
        cad_field_mapping={},
    )
    defaults.update(overrides)
    return Jurisdiction(**defaults)


def test_get_bpp_accounts_normalizes_candidates(monkeypatch):
    jurisdiction = make_jurisdiction()
    monkeypatch.setattr(
        cad_adapter,
        "fetch_candidate_records",
        lambda district_id: [MatchCandidate(record_id="r1", account_number="A1", owner_name="Acme LLC", tax_year=2026)],
    )

    adapter = cad_adapter.RenditionPilotCadAdapter()
    accounts = adapter.get_bpp_accounts(jurisdiction)

    assert len(accounts) == 1
    account = accounts[0]
    assert account.account_id == "r1"
    assert account.owner_name == "Acme LLC"
    assert account.jurisdiction_id == "jur-lubbock"
    # Documented data limitation: these fields simply don't exist yet.
    assert account.situs_address is None
    assert account.dba_name is None


def test_get_bpp_accounts_returns_empty_without_district(monkeypatch):
    jurisdiction = make_jurisdiction(district_id=None)
    adapter = cad_adapter.RenditionPilotCadAdapter()
    assert adapter.get_bpp_accounts(jurisdiction) == []


def test_get_bpp_accounts_degrades_gracefully_on_config_error(monkeypatch):
    jurisdiction = make_jurisdiction()

    def fail(*args, **kwargs):
        raise MatchingConfigError("boom")

    monkeypatch.setattr(cad_adapter, "fetch_candidate_records", fail)
    adapter = cad_adapter.RenditionPilotCadAdapter()

    assert adapter.get_bpp_accounts(jurisdiction) == []


def test_get_account_finds_by_id(monkeypatch):
    jurisdiction = make_jurisdiction()
    monkeypatch.setattr(
        cad_adapter,
        "fetch_candidate_records",
        lambda district_id: [
            MatchCandidate(record_id="r1", account_number="A1", owner_name="Acme LLC", tax_year=2026),
            MatchCandidate(record_id="r2", account_number="A2", owner_name="Other Co", tax_year=2026),
        ],
    )
    adapter = cad_adapter.RenditionPilotCadAdapter()

    assert adapter.get_account(jurisdiction, "r2").owner_name == "Other Co"
    assert adapter.get_account(jurisdiction, "missing") is None


def test_find_accounts_by_name_uses_name_similarity(monkeypatch):
    jurisdiction = make_jurisdiction()
    monkeypatch.setattr(
        cad_adapter,
        "fetch_candidate_records",
        lambda district_id: [
            MatchCandidate(record_id="r1", account_number="A1", owner_name="Acme Hardware LLC", tax_year=2026),
            MatchCandidate(record_id="r2", account_number="A2", owner_name="Totally Unrelated Co", tax_year=2026),
        ],
    )
    adapter = cad_adapter.RenditionPilotCadAdapter()

    matches = adapter.find_accounts_by_name(jurisdiction, "Acme Hardware")

    assert [m.account_id for m in matches] == ["r1"]


def test_find_accounts_by_address_returns_empty_documented_limitation(monkeypatch):
    jurisdiction = make_jurisdiction()
    adapter = cad_adapter.RenditionPilotCadAdapter()
    assert adapter.find_accounts_by_address(jurisdiction, "100 MAIN ST") == []


def test_property_lookups_return_no_data_documented_limitation():
    jurisdiction = make_jurisdiction()
    adapter = cad_adapter.RenditionPilotCadAdapter()
    assert adapter.get_real_property(jurisdiction, "R123456") is None
    assert adapter.find_property_by_situs(jurisdiction, "100 MAIN ST") == []


def test_available_fields_reflect_real_data_limitations():
    adapter = cad_adapter.RenditionPilotCadAdapter()
    assert adapter.AVAILABLE_ACCOUNT_FIELDS == frozenset({"owner_name", "account_number"})
    assert adapter.AVAILABLE_PROPERTY_FIELDS == frozenset()


def test_get_cad_adapter_returns_rendition_pilot_adapter():
    jurisdiction = make_jurisdiction()
    adapter = cad_adapter.get_cad_adapter(jurisdiction)
    assert isinstance(adapter, cad_adapter.RenditionPilotCadAdapter)
