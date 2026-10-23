from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from app.comptroller import mailing_account_links as links, matching

@pytest.fixture
def setup(monkeypatch):
    j=SimpleNamespace(district_id='district-2',county_name='TEST COUNTY',has_capability=lambda _:True)
    monkeypatch.setattr(links,'get_supabase_config',lambda:('https://db.test','key'))
    query=Mock(return_value=[{'id':'permit','address':'100 BUSINESS ST','zip':'79000','legal_name':'Acme LLC','location_name':'Acme'}])
    monkeypatch.setattr(links,'_request_json',query)
    match=Mock(return_value=matching.MatchResult('HIGH',1,'',matching.MatchCandidate('cad-id','P1','Acme',2027)))
    monkeypatch.setattr(matching,'match_imported_bpp_accounts',match)
    return j,query,match

def test_matches_business_situs_in_own_county_not_mailing_address(setup):
    j,query,match=setup
    result=links.account_link(j,'01000000001')
    assert result['matched_record_id']=='cad-id'
    assert query.call_args.kwargs['params']['county']=='eq.TEST COUNTY'
    assert query.call_args.kwargs['params']['taxpayer_id']=='eq.01000000001'
    assert match.call_args.kwargs['district_id']=='district-2'
    assert match.call_args.kwargs['address']=='100 BUSINESS ST'

@pytest.mark.parametrize('confidence,ambiguous',[('LOW',False),('MEDIUM',False),('HIGH',True)])
def test_unreliable_match_does_not_link(setup,confidence,ambiguous):
    j,_,match=setup
    match.return_value=matching.MatchResult(confidence,.8,'',matching.MatchCandidate('cad','P1','Acme',2027),ambiguous)
    assert links.account_link(j,'taxpayer')['matched_record_id'] is None

def test_multiple_different_accounts_are_not_arbitrarily_selected(setup):
    j,query,match=setup
    query.return_value*=2
    match.side_effect=[matching.MatchResult('HIGH',1,'',matching.MatchCandidate(pid,account,'Acme',2027)) for pid,account in [('cad1','P1'),('cad2','P2')]]
    assert links.account_link(j,'taxpayer')['matched_record_id'] is None

def test_no_cad_capability_never_creates_a_match(setup):
    j,query,_=setup;j.has_capability=lambda _:False
    assert links.account_link(j,'taxpayer')['matched_record_id'] is None
    query.assert_not_called()
