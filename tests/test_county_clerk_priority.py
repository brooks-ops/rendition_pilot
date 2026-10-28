from copy import deepcopy
import pytest
from app.county_clerk.priority import filing_priority


def evidence():
    return {'filing':{'assumed_name':'ACME SHOP LLC','owners':['SMITH JANE'],'owner_name_complete':True,'business_address':{'line1':'100 MAIN ST','zip':'79401'}},'comparison':{'candidate_details':{'property_id':'id','real_account_number':'P1','business_name':'ACME SHOP','owner_name':'SMITH, JANE','situs_address_raw':'100 MAIN STREET, LUBBOCK, TX','situs_zip':'79401'}}}


def test_exact_normalized_address_zip_and_identity_is_high():
    assert filing_priority(evidence=evidence(),account='P1',record_id='id')[:2]==(0,'HIGH')

@pytest.mark.parametrize('change',['partial_name','address','zip','suite','ambiguous','wrong_account','wrong_pid','no_pid'])
def test_non_exact_matches_are_capped_at_medium(change):
    e=evidence();d=e['comparison']['candidate_details'];kwargs={'account':'P1','record_id':'id'}
    if change=='partial_name':d.update(business_name='ACME SHOP SOUTH',owner_name='SMITH JANET')
    if change=='address':d['situs_address_raw']='200 MAIN STREET'
    if change=='zip':d['situs_zip']=None
    if change=='suite':d['situs_address_raw']='100 MAIN ST STE 2'
    if change=='ambiguous':kwargs['ambiguous']=True
    if change=='wrong_account':kwargs['account']='P2'
    if change=='wrong_pid':kwargs['record_id']='other'
    if change=='no_pid':kwargs['record_id']=None
    assert filing_priority(evidence=e,**kwargs)[1]=='MEDIUM'


def test_name_only_and_unmatched_are_not_high():
    e=evidence();e['comparison']={};e['account_candidates']=[{'id':'id','real_account_number':'P1','owner_name':'SMITH JANE'}]
    assert filing_priority(evidence=e)[1]=='MEDIUM'
    assert filing_priority(evidence={'filing':{'assumed_name':'NEW SHOP'}})[1]=='LOW'
