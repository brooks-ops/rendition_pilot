from datetime import datetime, timezone, date
from types import SimpleNamespace
import pytest
from app.county_clerk import quarterly, collection_status, service
from app.county_clerk.schedule import schedule, window_for

J = SimpleNamespace(id='j', district_id='d')
SOURCE = {'id': 's', 'district_id': 'd', 'jurisdiction_id': 'j', 'county': 'LUBBOCK'}

@pytest.mark.parametrize('day,start,end,next_day', [
    ('2026-01-01', '2025-01-01', '2025-12-31', '2026-04-01'),
    ('2026-04-01', '2026-01-01', '2026-03-31', '2026-07-01'),
    ('2026-07-01', '2026-01-01', '2026-06-30', '2026-10-01'),
    ('2026-10-01', '2026-01-01', '2026-09-30', '2027-01-01'),
    ('2026-10-28', '2026-01-01', '2026-09-30', '2027-01-01'),
])
def test_quarterly_year_to_date_and_catch_up(day, start, end, next_day):
    result = schedule(date.fromisoformat(day))
    assert result['pending_window']['start'] == start
    assert result['pending_window']['end'] == end
    assert result['next_run']['scheduled_for'] == next_day
    assert result['october_readiness_window']['start'] == day[:4]+'-01-01'
    assert result['october_readiness_window']['end'] == day[:4]+'-09-30'


def test_quarter_boundary_uses_chicago_not_utc():
    before = schedule(datetime(2026,10,1,4,59,tzinfo=timezone.utc))
    after = schedule(datetime(2026,10,1,5,0,tzinfo=timezone.utc))
    assert before['pending_window']['scheduled_for'] == '2026-07-01'
    assert after['pending_window']['scheduled_for'] == '2026-10-01'
    assert window_for('2024-04-01')['end'] == '2024-03-31'
    with pytest.raises(ValueError):schedule(datetime(2026,10,1))
    with pytest.raises(ValueError):window_for('2026-10-02')


def test_enabled_flags_cannot_turn_planning_into_source_retrieval(monkeypatch):
    calls=[]
    def request(method, table, **kwargs):
        calls.append((method,table,kwargs))
        if method == 'GET':return [{**SOURCE,'automation_authorized':True,'automation_enabled':True}]
        return {'id':'r','district_id':'d','jurisdiction_id':'j','status':'BLOCKED','records_retrieved':None}
    monkeypatch.setattr(quarterly, 'request', request)
    r=quarterly.check_quarterly(J,now=date(2026,10,28))
    assert r['status']=='BLOCKED' and r['records_retrieved'] is None
    assert calls[0][2]['params']['district_id']=='eq.d'
    assert calls[0][2]['params']['jurisdiction_id']=='eq.j'
    assert calls[1][1]=='rpc/dba_plan_quarterly_run'
    assert calls[1][2]['payload']=={'p_source_id':'s','p_scheduled_for':'2026-10-01'}


def test_dry_run_does_not_write_and_unknown_county_has_no_fallback(monkeypatch):
    def request(method,*args,**kwargs):
        assert method=='GET'
        return [SOURCE]
    monkeypatch.setattr(quarterly,'request',request)
    assert quarterly.check_quarterly(J,dry_run=True,now=date(2026,10,1))['status']=='DRY_RUN'
    monkeypatch.setattr(quarterly,'request',lambda *a,**kw:[])
    assert quarterly.check_quarterly(J,now=date(2026,10,1))['status']=='NOT_CONFIGURED'


def test_wrong_district_plan_is_rejected(monkeypatch):
    monkeypatch.setattr(quarterly,'request',lambda method,*a,**kw:[SOURCE] if method=='GET' else {'id':'r','district_id':'foreign','jurisdiction_id':'j','status':'BLOCKED'})
    with pytest.raises(ValueError,match='another district'):
        quarterly.check_quarterly(J,now=date(2026,10,1))


def test_blocked_october_plan_never_shows_full_year_ready(monkeypatch):
    def request(method,table,**kwargs):
        assert kwargs['params']['district_id']=='eq.d'
        assert kwargs['params']['source_id']=='eq.s'
        return [{'scheduled_for':'2026-10-01','status':'BLOCKED','window_start':'2026-01-01','window_end':'2026-09-30'}]
    monkeypatch.setattr(service,'request',request)
    r=collection_status.for_source(SOURCE,now=date(2026,10,28))
    assert r['cadence']=='quarterly' and r['status']=='BLOCKED'
    assert not r['october_collection_complete']
    assert r['last_successful_sync'] is None and not r['automation_enabled']
    assert 'have not been collected' in r['message']


def test_collection_status_uses_authenticated_district(monkeypatch):
    from fastapi import FastAPI,HTTPException
    from fastapi.testclient import TestClient
    from app.county_clerk.api import router
    app=FastAPI()
    def auth(token):
        if token!='own':raise HTTPException(403,'No BPP access')
        return SimpleNamespace(district_id='own-county')
    monkeypatch.setattr(collection_status,'for_district',lambda district:{'district_id':district,'cadence':'quarterly'})
    app.include_router(router(auth))
    client=TestClient(app)
    assert client.post('/api/county-clerk/collection-status',json={'access_token':'bad'}).status_code==403
    r=client.post('/api/county-clerk/collection-status',json={'access_token':'own','district_id':'foreign'})
    assert r.json()=={'district_id':'own-county','cadence':'quarterly'}
