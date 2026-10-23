from types import SimpleNamespace
import pytest
from app.comptroller import matching, jurisdictions, property_adapter
from tests.test_property_matching import make_property

@pytest.fixture
def imported(monkeypatch):
    jurisdiction = SimpleNamespace(id='jur-1', account_type_prefixes={'personal':'P','real':'R'}, has_capability=lambda _: True)
    monkeypatch.setattr(jurisdictions, 'get_jurisdiction_by_district_id', lambda _: jurisdiction)
    rows = []
    monkeypatch.setattr(property_adapter, 'get_property_adapter', lambda _: SimpleNamespace(find_properties_by_address=lambda *a, **k: rows))
    return rows

def run():
    return matching.match_closure_to_account(district_id='district-1', permit_address='100 MAIN ST', permit_zip='79401', permit_legal_name='ACME LLC', permit_location_name='ACME', candidates=[])

def test_matches_imported_p_account_without_renditions(imported):
    imported.extend([make_property('land','100 MAIN ST','79401','R1',owner_name='ACME'), make_property('business','100 MAIN ST','79401','P1',owner_name='ACME')])
    result = run()
    assert result.confidence == 'HIGH'
    assert result.candidate.account_number == 'P1'
    assert result.signals['cad_account_source'] == 'real_property_records'
    assert 'imported CAD' in result.signals['existing_rendition_record']

def test_land_or_unrelated_owner_never_becomes_bpp_match(imported):
    imported.extend([make_property('land','100 MAIN ST','79401','R1',owner_name='ACME'), make_property('other','100 MAIN ST','79401','P2',owner_name='XYZ INDUSTRIES')])
    assert run().confidence == "LOW"
    assert run().candidate.account_number == "P2"

def test_shared_situs_competing_names_are_ambiguous(imported):
    imported.extend([make_property('a','100 MAIN ST','79401','P1',owner_name='ACME'),make_property('b','100 MAIN ST','79401','P2',owner_name='ACME')])
    result = run()
    assert result.ambiguous
    assert result.confidence == 'LOW'

def test_suite_and_zip_conflicts_exclude_account(imported):
    imported.append(make_property('a','100 MAIN ST','79999','P1',owner_name='ACME'))
    assert run().candidate is None
    imported[:] = [make_property('a','100 MAIN ST STE 200','79401','P1',owner_name='ACME')]
    result = matching.match_closure_to_account(district_id='district-1',permit_address='100 MAIN ST STE 100',permit_zip='79401',permit_legal_name='ACME',permit_location_name='ACME',candidates=[])
    assert result.candidate is None


def test_missing_owner_is_address_only_low_confidence(imported):
    imported.append(make_property('a','100 MAIN ST','79401','P1'))
    result = run()
    assert result.confidence == 'LOW'
    assert result.candidate.account_number == 'P1'
    assert 'address alone' in result.reason


def test_missing_names_at_shared_situs_do_not_pick_a_business(imported):
    imported.extend([make_property('a','100 MAIN ST','79401','P1'),make_property('b','100 MAIN ST','79401','P2')])
    result = run()
    assert result.ambiguous
    assert result.candidate is None
    assert result.signals['personal_property_accounts'] == 'P1, P2'
