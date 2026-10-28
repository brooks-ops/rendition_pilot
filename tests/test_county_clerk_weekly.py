from datetime import datetime,timezone
from types import SimpleNamespace
from app.county_clerk import detailed, import_captured, captured
from app.comptroller.matching import MatchResult,MatchCandidate
import pytest

J=SimpleNamespace(id='j',district_id='d',account_type_prefixes={'personal':'P','real':'R'})

@pytest.mark.parametrize('confidence,linked',[('HIGH','P1'),('MEDIUM',None),('LOW',None)])
def test_detailed_classification_uses_only_strong_links(monkeypatch,confidence,linked):
    r={'assumed_name':'SHOP','owners':['OWNER'],'owner_name':'OWNER','business_address':{'line1':'100 MAIN','zip':'79401'},'filing_type':'ASSUMED NAME','document_number':'123'}
    monkeypatch.setattr(detailed,'match_imported_bpp_accounts',lambda **kw:MatchResult(confidence,.9,'evidence',MatchCandidate('id','P1','OWNER',2026)))
    monkeypatch.setattr(detailed,'get_property_adapter',lambda j:SimpleNamespace(get_property_by_id=lambda *a:None))
    out=detailed.classify_detail(r,{},J)
    assert out['matched_account_number']==linked
    assert out['evidence']['filing']==r
    import json
    r['intelligence']=out
    json.dumps(r)
    r.pop('intelligence')
    rejected=detailed.classify_detail(r,{},J,rejected_accounts={'P1'})
    assert rejected['matched_account_number'] is None

def test_structured_import_refuses_incomplete_index(tmp_path):
    (tmp_path/'index.json').write_text('{"text":"101 items found, displaying 1 to 100", "records":[]}')
    with pytest.raises(ValueError,match='incomplete'):import_captured.prepare(tmp_path,{'county':'LUBBOCK','state':'TX'},'2026-10-01','2026-10-28')

def test_source_address_projects_into_regular_queue():
    from app.comptroller.intelligence import _from_intelligence_row
    row={'id':'x','signal_type':'county_clerk_dba','evidence':{'filing':{'business_address':{'line1':'100 MAIN','line2':'STE 2','city':'LUBBOCK','state':'TX','zip':'79401'}}}}
    item=_from_intelligence_row(row)
    assert item.source_address=='100 MAIN STE 2' and item.source_city=='LUBBOCK'
    assert 'source_address' not in row
