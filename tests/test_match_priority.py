import pytest
from app.comptroller.priority import match_priority

@pytest.mark.parametrize('signals,ambiguous,expected',[
 ({'address':'MATCH','business_dba_name':'MATCH'},False,(0,'HIGH')),
 ({'address':'NORMALIZED MATCH','legal_entity_name':'MATCH'},False,(0,'HIGH')),
 ({'address':'MATCH','business_dba_name':'PARTIAL MATCH'},False,(1,'MEDIUM')),
 ({'address':'MATCH'},False,(2,'MEDIUM')),
 ({'business_dba_name':'MATCH'},False,(3,'MEDIUM')),
 ({'address':'MATCH','business_dba_name':'MATCH'},True,(4,'MEDIUM')),
 ({'address':'MATCH','business_dba_name':'MATCH','zip':'NO MATCH'},False,(4,'MEDIUM')),
])
def test_evidence_hierarchy(signals,ambiguous,expected):
    assert match_priority(account='P1',record_id='cad-id',signals=signals,ambiguous=ambiguous)[:2]==expected

def test_account_number_without_pid_cannot_claim_full_match():
    assert match_priority(account='P1',signals={'address':'MATCH','business_dba_name':'MATCH'})[1]!='HIGH'

def test_unmatched_is_last():
    assert match_priority(account=None)[:2]==(5,'LOW')
