from types import SimpleNamespace
import csv
import io
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from app import activity_analytics as analytics
from backend import main


def context(role='admin', district_id='district-one'):
    return SimpleNamespace(district_id=district_id, user_id='verified-user', email='appraiser@cad.test', role=role)


def test_activity_contains_verified_identity_and_no_sensitive_work_payload(monkeypatch):
    written=[]
    monkeypatch.setattr(analytics,'get_supabase_config',lambda:('https://db.invalid','key'))
    monkeypatch.setattr(analytics,'_request_json',lambda *a,**kw:written.append(kw['json_payload']))
    analytics.record_activity(context(),'review_completed','mailing_changes','bpp_intelligence_items','item-one',outcome='ADDRESS_UPDATED')
    assert written[0]['actor_id']=='verified-user'
    assert written[0]['district_id']=='district-one'
    assert set(written[0])=={'district_id','actor_id','actor_email','action','module','entity_type','entity_id','outcome','units'}
    analytics.record_activity(context(),'commercial_update_applied','commercial','batch','batch-one',dedup_key='batch-one')
    analytics.record_activity(context(),'commercial_update_applied','commercial','batch','batch-one',dedup_key='batch-one')
    assert written[1]['dedup_key']==written[2]['dedup_key']
    analytics.record_activity(context(district_id='district-two'),'commercial_update_applied','commercial','batch','batch-one',dedup_key='batch-one')
    assert written[1]['dedup_key']!=written[3]['dedup_key']


def test_telemetry_failure_does_not_fail_saved_work(monkeypatch,caplog):
    monkeypatch.setattr(analytics,'get_supabase_config',lambda:('https://db.invalid','key'))
    def offline(*a,**kw):raise RuntimeError('PRIVATE provider error')
    monkeypatch.setattr(analytics,'_request_json',offline)
    analytics.record_activity(context(),'review_completed','business_intelligence','item','one')
    assert 'could not be recorded' in caplog.text
    assert 'PRIVATE' not in caplog.text


def test_report_uses_verified_district_and_members_cannot_read(monkeypatch):
    monkeypatch.setattr(main,'get_authenticated_district_context',lambda token:context('member' if token=='member' else 'admin'))
    calls=[]
    monkeypatch.setattr(analytics,'report',lambda district,start,end,**kw:calls.append((district,start,end,kw['workspace'])) or {'totals':{},'appraisers':[]})
    client=TestClient(main.app)
    payload={'access_token':'member','start':'2026-10-01','end':'2026-10-23','district_id':'attacker-selected'}
    assert client.post('/api/admin/activity',json=payload).status_code==403
    assert calls==[]
    response=client.post('/api/admin/activity',json={**payload,'access_token':'admin'})
    assert response.status_code==200
    assert calls==[('district-one','2026-10-01','2026-10-23','all')]
    assert response.headers['cache-control']=='no-store'


def test_invalid_ranges_do_not_query_database(monkeypatch):
    monkeypatch.setattr(analytics,'get_supabase_config',lambda:pytest.fail('Invalid date range reached database'))
    for start,end in [('bad','2026-10-23'),('2026-10-24','2026-10-23'),('2020-01-01','2026-01-01')]:
        with pytest.raises(ValueError):analytics.report('district-one',start,end)


def test_sales_export_excludes_staff_identity_and_account_details():
    report={'start':'2026-10-01','end':'2026-10-23','tracking_started_at':'2026-10-23','totals':{'completed':7,'active_appraisers':2},'appraisers':[{'appraiser':'private@cad.test','actor_id':'private-id','completed':7}],'outcomes':[{'entity_id':'P123PRIVATE'}]}
    sales=analytics.csv_report(report,sales=True)
    assert 'completed,7' in sales
    assert 'private' not in sales.lower()
    assert 'P123' not in sales
    report['appraisers'][0]['appraiser']='=FORMULA()'
    rows=list(csv.reader(io.StringIO(analytics.csv_report(report))))
    assert rows[1][0]=="'=FORMULA()"


def test_successful_review_attributed_but_failed_mutation_not_counted(monkeypatch):
    from tests.test_backend_intelligence_queue import make_item
    district=context('member')
    monkeypatch.setattr(main,'require_bpp_access',lambda token:district)
    monkeypatch.setattr(main,'_require_own_district_item',lambda *args:make_item())
    monkeypatch.setattr(main,'get_supabase_user',lambda token:{'id':'verified-user'})
    monkeypatch.setattr(main.comptroller_intelligence,'resolve_item',lambda *a,**kw:make_item(signal_type='mailing_address_change',resolution='ADDRESS_UPDATED'))
    events=[]
    monkeypatch.setattr(analytics,'record_activity',lambda *a,**kw:events.append((a,kw)))
    request=main.IntelligenceResolveRequest(access_token='token',source_table='bpp_intelligence_items',item_id='intel-1',resolution='ADDRESS_UPDATED')
    main.intelligence_resolve(request)
    assert events[0][0][:3]==(district,'review_completed','mailing_changes')
    def failed(*a,**kw):raise main.comptroller_intelligence.IntelligenceQueueError('save failed')
    monkeypatch.setattr(main.comptroller_intelligence,'resolve_item',failed)
    with pytest.raises(HTTPException):main.intelligence_resolve(request)
    assert len(events)==1


def test_rendition_lock_tracks_header_authenticated_actor_only_on_success(monkeypatch):
    monkeypatch.setattr(main,'get_authenticated_district_context',lambda token:context('member'))
    monkeypatch.setattr(main,'require_access',lambda district,workspace:district)
    monkeypatch.setattr(main,'build_final_review_record',lambda **kw:{'account_number':kw['account_number'],'decision':kw['decision']})
    events=[]
    monkeypatch.setattr(analytics,'record_activity',lambda *a,**kw:events.append((a,kw)))
    client=TestClient(main.app)
    body={'file_name':'test.pdf','result':{'metadata':{'tax_year':2027}},'final_value':100,'final_source':'manual_override','appraiser_initials':'XX','account_number':'p123','district_context':{'district_id':'spoofed','user_id':'spoofed'}}
    response=client.post('/api/review/lock',headers={'Authorization':'Bearer real-user'},json=body)
    assert response.status_code==200
    assert events[0][0][0].user_id=='verified-user'
    assert events[0][0][0].district_id=='district-one'
    assert events[0][0][1:] == ('rendition_locked','rendition','rendition_account_year','2027:P123')
    assert client.post('/api/review/lock',headers={'Authorization':'Bearer real-user'},json={**body,'account_number':''}).status_code==400
    assert len(events)==1


def test_commercial_counts_committed_batch_not_preview_or_failed_update(monkeypatch):
    import importlib
    from fastapi import FastAPI
    api=importlib.import_module('app.commercial.api')
    class FakeStore:
        def preview(self,payload):return {'count':2}
        def commit(self,token,actor,reason):
            if token=='bad':raise ValueError('Invalid token')
            return {'batch_id':'saved-batch','updated':2}
    monkeypatch.setattr(api,'load_store',lambda *args:FakeStore())
    events=[]
    monkeypatch.setattr(api,'record_activity',lambda *a,**kw:events.append((a,kw)))
    app=FastAPI();app.include_router(api.commercial_router(lambda token:context()))
    client=TestClient(app)
    assert client.post('/api/commercial/preview',json={}).status_code==200
    assert client.post('/api/commercial/apply',json={'token':'bad'}).status_code==400
    assert events==[]
    assert client.post('/api/commercial/apply',json={'token':'good'}).status_code==200
    assert events[0][0][1:] == ('commercial_update_applied','commercial','commercial_batch','saved-batch')
    assert events[0][1] == {'units':2,'dedup_key':'saved-batch'}


def test_commercial_report_scope_carries_through_to_rpc_and_exports(monkeypatch):
    monkeypatch.setattr(analytics,'get_supabase_config',lambda:('https://db.invalid','key'))
    captured=[]
    monkeypatch.setattr(analytics,'_request_json',lambda *a,**kw:captured.append((a,kw)) or {'workspace':'commercial'})
    assert analytics.report('verified-district','2026-10-01','2026-10-23','commercial')['workspace']=='commercial'
    assert captured[0][0][1].endswith('/rpc/appraiser_activity_report_v2')
    assert captured[0][1]['json_payload']=={'p_district_id':'verified-district','p_start':'2026-10-01','p_end':'2026-10-23','p_workspace':'commercial'}
    with pytest.raises(ValueError):analytics.report('verified-district','2026-10-01','2026-10-23','unknown')
    data={'workspace':'commercial','start':'2026-10-01','end':'2026-10-23','totals':{'commercial_rows_updated':50},'appraisers':[{'appraiser':'name@cad.test','commercial_updates':1,'commercial_rows_updated':50}]}
    exported=analytics.csv_report(data)
    assert 'commercial_rows_updated' in exported
    assert 'completed' not in next(csv.reader(io.StringIO(exported)))
    sales=analytics.csv_report(data,sales=True)
    assert 'commercial_rows_updated,50' in sales
    assert 'name@cad.test' not in sales
    assert 'commercial,2026-10-01,2026-10-23' in sales


def test_commercial_scope_remains_admin_only(monkeypatch):
    monkeypatch.setattr(main,'get_authenticated_district_context',lambda token:context('member' if token=='member' else 'admin'))
    calls=[]
    monkeypatch.setattr(analytics,'report',lambda district,start,end,**kw:calls.append((district,kw['workspace'])) or {'workspace':'commercial','totals':{},'appraisers':[]})
    client=TestClient(main.app)
    body={'access_token':'member','workspace':'commercial','start':'2026-10-01','end':'2026-10-23'}
    assert client.post('/api/admin/activity',json=body).status_code==403
    assert calls==[]
    assert client.post('/api/admin/activity',json={**body,'access_token':'admin','district_id':'spoofed'}).status_code==200
    assert calls==[('district-one','commercial')]
    assert client.post('/api/admin/activity',json={**body,'workspace':'unknown'}).status_code==422


def test_equity_analysis_records_only_successful_generation(monkeypatch):
    import importlib
    from fastapi import FastAPI
    api=importlib.import_module('app.commercial.api')
    monkeypatch.setattr(api,'load_store',lambda *args:object())
    def equity(store,record_id):
        if record_id=='bad':raise KeyError('not found')
        return {'subject':{'id':record_id}}
    monkeypatch.setattr('app.commercial.equity.generate_equity',equity)
    events=[]
    monkeypatch.setattr(api,'record_activity',lambda *a,**kw:events.append((a,kw)))
    app=FastAPI();app.include_router(api.commercial_router(lambda token:context()))
    client=TestClient(app)
    assert client.post('/api/commercial/equity',json={'id':'bad'}).status_code==404
    assert events==[]
    assert client.post('/api/commercial/equity',json={'id':'good'}).status_code==200
    assert events[0][0][1:]==('commercial_equity_generated','commercial','commercial_property','good')


@pytest.fixture(autouse=True)
def isolated_work_claims(monkeypatch):
    monkeypatch.setattr(main.bpp_work_claims,'claim',lambda *a:None)
    monkeypatch.setattr(main.bpp_work_claims,'claims',lambda *a:{})
    monkeypatch.setattr(main.bpp_work_claims,'release',lambda *a,**kw:None)
