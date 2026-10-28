"""Protect evidence boundaries in the isolated browser-capture experiment."""
from types import SimpleNamespace
import pytest
from prototypes.lubbock_dba import prototype as p
from app.comptroller.matching import MatchResult,MatchCandidate

def field(label,value):return f'<span class="fieldLabel">{label}</span><br><span class="field"><span class="text">{value}</span></span>'
def capture(kind='ASSUMED NAME',number='2026701212',with_address=True):
    address=field('Business Address','<table><tr><td>'+field('Address1','100 MAIN ST')+field('City','LUBBOCK')+field('State','TX')+field('Zip','79401')+'</td></tr></table>') if with_address else ''
    content='<div id="presentation">'+field('Document Number',number)+field('Filing Date','10/19/2026 10:06:30 AM')+'<fieldset><legend>AssumedName</legend>'+field('Name','EXAMPLE SHOP')+address+'</fieldset><fieldset><legend>Owner(s)</legend><table><tr><th>Name(s)</th><th>Owner Address</th></tr><tr><td>OWNER ONE</td><td>999 PRIVATE ST</td></tr><tr><td>OWNER TWO</td><td>888 OTHER ST</td></tr></table></fieldset></div>'
    return {'url':'https://erecord.lubbockcounty.gov/recorder/eagleweb/viewDoc.jsp?node=DOC1','html':content,'body':kind+' - '+number+" Images are available at the County Clerk's office."},{'description':kind+'\n'+number}

def test_full_owners_and_business_address_without_invented_mailing():
    r=p.parse_detail(*capture())
    assert r['owners']==['OWNER ONE','OWNER TWO']
    assert r['owner_name'] is None and r['owner_name_complete']
    assert r['business_address']['line1']=='100 MAIN ST'
    assert r['mailing_address'] is None and r['expiration_date'] is None
    assert r['images_available_online'] is False
    assert '999 PRIVATE ST' not in str(r)

def test_abandonment_keeps_missing_address():
    r=p.parse_detail(*capture(kind='ASSUMED NAME ABANDONMENT',with_address=False))
    assert r['business_address'] is None
    assert r['filing_type']=='ASSUMED NAME ABANDONMENT'
    assert r['recording_date']=='2026-10-19'

@pytest.mark.parametrize('change',['foreign','number','type','layout'])
def test_source_or_layout_mismatches_stop_extraction(change):
    c,i=capture()
    if change=='foreign':c['url']=c['url'].replace('erecord.lubbockcounty.gov','foreign.example')
    elif change=='number':i['description']='ASSUMED NAME\n2026709999'
    elif change=='type':i['description']='ASSUMED NAME ABANDONMENT\n2026701212'
    else:c['html']='<div>Unrecognized page</div>'
    with pytest.raises(ValueError):p.parse_detail(c,i)

def test_exact_name_never_promotes_land_or_inactive_accounts():
    r=p.parse_detail(*capture());j=SimpleNamespace(account_type_prefixes={'personal':'P','real':'R'})
    base={'id':'1','business_name':'EXAMPLE SHOP','tax_year':2026}
    ctx={'accounts':[{**base,'real_account_number':'R1'},{**base,'real_account_number':'P2','account_status':'NOT_IN_ACTIVE_DBA_EXPORT'},{**base,'real_account_number':'P3'},{**base,'real_account_number':'P3','tax_year':2025}]}
    candidates=p.name_candidates(r,ctx,j)
    assert [c['real_account_number'] for c in candidates]==['P3']
    assert candidates[0]['confidence']=='MEDIUM'

def test_different_owners_linking_different_accounts_require_review(monkeypatch):
    r=p.parse_detail(*capture());j=SimpleNamespace(district_id='own',account_type_prefixes={'personal':'P','real':'R'})
    def match(**kwargs):
        n='P1' if kwargs['legal_name']=='OWNER ONE' else 'P2'
        return MatchResult('HIGH',1.0,'Address and owner match',MatchCandidate(n,n,kwargs['legal_name'],2026))
    monkeypatch.setattr(p,'match_imported_bpp_accounts',match)
    monkeypatch.setattr(p,'get_property_adapter',lambda j:SimpleNamespace(get_property_by_id=lambda *args:None))
    out=p.compare(r,{},j)
    assert out['matched_account'] is None and out['status']=='NEEDS_REVIEW' and out['confidence']=='LOW'

def test_partial_name_same_address_is_not_a_strong_account_link(monkeypatch):
    r=p.parse_detail(*capture());r['owners']=['WALLACE BABY']
    j=SimpleNamespace(district_id='own',account_type_prefixes={'personal':'P','real':'R'})
    monkeypatch.setattr(p,'match_imported_bpp_accounts',lambda **kw:MatchResult('MEDIUM',.65,'Address with partial owner name',MatchCandidate('id','P1','WALLACE ALLEN',2026),signals={'legal_entity_name':'PARTIAL MATCH','address':'MATCH'}))
    monkeypatch.setattr(p,'get_property_adapter',lambda j:SimpleNamespace(get_property_by_id=lambda *args:None))
    out=p.compare(r,{},j)
    assert out['status']=='NEEDS_REVIEW' and out['matched_account'] is None
    assert out['production_match']['candidate']['account_number']=='P1'
