from types import SimpleNamespace
import pytest
from fastapi import FastAPI,HTTPException
from fastapi.testclient import TestClient
from app.county_clerk import sync_status,service
from app.county_clerk.api import router

def test_counts_are_district_scoped(monkeypatch):
    seen=[]
    monkeypatch.setattr(service,'get_supabase_config',lambda:('https://database.example','server-key'))
    monkeypatch.setattr(service,'postgrest_headers',lambda key,prefer:{'Prefer':prefer})
    def get(url,**kwargs):
        seen.append((url,kwargs))
        return SimpleNamespace(headers={'Content-Range':'0-0/12'},raise_for_status=lambda:None)
    monkeypatch.setattr(service.requests,'get',get)
    assert sync_status.count_rows('county_clerk_dba_filings','district-a',filing_type='eq.ASSUMED NAME')==12
    assert seen[0][1]['params']['district_id']=='eq.district-a'
    assert seen[0][1]['params']['limit']==1

def test_dashboard_never_calls_pdf_import_a_sync(monkeypatch):
    monkeypatch.setattr(service,'history',lambda district:{'source':{'county':'LUBBOCK','state':'TX','last_successful_import_at':'2026-10-28','last_successful_retrieval_at':None,'automation_enabled':True},'imports':[{'id':'i','filename':'partial.pdf','status':'PARTIAL'}],'next_window':{'start':None}})
    calls=[]
    def count(table,district_id,**filters):calls.append((table,district_id,filters));return 0
    monkeypatch.setattr(sync_status,'count_rows',count)
    d=sync_status.dashboard('district-a')
    assert d['last_successful_sync'] is None and d['last_successful_fallback_import']=='2026-10-28'
    assert not d['automation_enabled'] and not d['manual_sync_available']
    assert d['connection_status']=='AWAITING_APPROVED_CONNECTION'
    assert len(calls)==6 and all(x[1]=='district-a' for x in calls)
    assert d['metrics']['new_filings']==0 and 'including PDF' in d['metrics_scope']
    assert d['fallback_import_issues'][0]['status']=='PARTIAL'

def test_unconfigured_district_has_no_fallback_county(monkeypatch):
    def history(district):raise service.ImportError('No source for this district')
    monkeypatch.setattr(service,'history',history)
    monkeypatch.setattr(sync_status,'count_rows',lambda *a,**kw:pytest.fail('No cross-county fallback'))
    assert sync_status.dashboard('unconfigured')['configured'] is False

def test_status_requires_bpp_and_uses_authenticated_county(monkeypatch):
    app=FastAPI()
    def auth(token):
        if token!='own':raise HTTPException(403,'No BPP')
        return SimpleNamespace(district_id='own-county')
    monkeypatch.setattr(sync_status,'dashboard',lambda district:{'district_id':district})
    app.include_router(router(auth));client=TestClient(app)
    assert client.post('/api/county-clerk/sync-status',json={'access_token':'bad'}).status_code==403
    r=client.post('/api/county-clerk/sync-status',json={'access_token':'own','district_id':'foreign'})
    assert r.json()=={'district_id':'own-county'}

def test_unknown_count_is_not_presented_as_zero(monkeypatch):
    monkeypatch.setattr(service,'get_supabase_config',lambda:('https://database.example','server-key'))
    monkeypatch.setattr(service,'postgrest_headers',lambda *a,**kw:{})
    monkeypatch.setattr(service.requests,'get',lambda *a,**kw:SimpleNamespace(headers={'Content-Range':'0-0/*'},raise_for_status=lambda:None))
    with pytest.raises(service.ComptrollerServiceError):sync_status.count_rows('county_clerk_dba_filings','own')
