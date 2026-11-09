"""Staff may see only assigned work; every API read/write uses server identity."""
from dataclasses import replace
from unittest.mock import Mock
from uuid import uuid4
import pytest
from fastapi import HTTPException
from backend import main
from app import bpp_visibility as visibility,bpp_assignments
from app.comptroller import intelligence
from tests.test_backend_intelligence_queue import make_district,make_item

@pytest.fixture
def member(monkeypatch):
    context=make_district(role='member')
    monkeypatch.setattr(main,'require_bpp_access',lambda _:context)
    monkeypatch.setattr(main.bpp_work_claims,'attach',lambda rows,_:rows)
    monkeypatch.setattr(intelligence,'reviewer_labels',lambda *_:{})
    return context

@pytest.mark.parametrize('role',['member','appraiser','district_admin'])
@pytest.mark.parametrize('requested',[None,'ME','UNASSIGNED','someone-else'])
def test_staff_cannot_widen_scope_through_filters(role,requested):
    context=make_district(role=role)
    assert visibility.assignment_filter(context,requested)==context.user_id


def test_admin_retains_all_and_named_filters():
    context=make_district()
    assert visibility.assignment_filter(context) is None
    assert visibility.assignment_filter(context,'ME')==context.user_id
    assert visibility.assignment_filter(context,'UNASSIGNED')=='UNASSIGNED'
    assert visibility.assignment_filter(context,'other')=='other'

@pytest.mark.parametrize('signal',[None,'new_business','sales_tax_inactive','mailing_address_change'])
@pytest.mark.parametrize('requested',[None,'UNASSIGNED','someone-else'])
def test_queue_forces_authenticated_assignment_for_every_signal(member,monkeypatch,signal,requested):
    listing=Mock(return_value=[]);monkeypatch.setattr(intelligence,'list_intelligence_queue',listing)
    monkeypatch.setattr('app.comptroller.mailing_comparisons.latest_run',lambda _:None)
    main.intelligence_queue(main.IntelligenceQueueRequest(access_token='token',signal_type=signal,assigned_to=requested))
    assert listing.call_args.kwargs['assigned_to']==member.user_id


def test_summary_scoped_to_staff_assignments(member,monkeypatch):
    summary=Mock(return_value={});monkeypatch.setattr(intelligence,'get_queue_summary',summary)
    main.intelligence_summary(main.ComptrollerStatusRequest(access_token='token'))
    assert summary.call_args.kwargs['assigned_to']==member.user_id

@pytest.mark.parametrize('owner',[None,'someone-else'])
@pytest.mark.parametrize('endpoint',[main.intelligence_item_detail,main.intelligence_investigate,main.intelligence_dismiss,main.intelligence_account_card])
def test_other_or_unassigned_item_cannot_be_opened_or_changed(member,monkeypatch,owner,endpoint):
    monkeypatch.setattr(intelligence,'get_intelligence_item',lambda *_:make_item(raw={'assigned_to':owner}))
    cls=main.IntelligenceInvestigateRequest if endpoint==main.intelligence_investigate else main.IntelligenceDismissRequest if endpoint==main.intelligence_dismiss else main.IntelligenceItemRequest
    with pytest.raises(HTTPException) as exc:
        endpoint(cls(access_token='token',source_table='bpp_intelligence_items',item_id='item'))
    assert exc.value.status_code==403


def test_own_item_visible_and_admin_can_view_any(monkeypatch,member):
    monkeypatch.setattr(intelligence,'get_intelligence_item',lambda *_:make_item(raw={'assigned_to':member.user_id}))
    result=main.intelligence_item_detail(main.IntelligenceItemRequest(access_token='token',source_table='bpp_intelligence_items',item_id='item'))
    assert result['item']['raw']['assigned_to']==member.user_id
    assert visibility.can_view_item(make_district(),make_item(raw={'assigned_to':'someone-else'}))


def test_staff_without_identity_fail_closed():
    with pytest.raises(visibility.AssignmentAccessError):visibility.assignment_filter(replace(make_district(role='member'),user_id=None))
    assert not visibility.can_view_item(replace(make_district(role='member'),user_id=None),make_item())


def test_both_source_queries_apply_database_assignment_filter(monkeypatch):
    monkeypatch.setattr(intelligence,'get_supabase_config',lambda:('https://db.example','secret'))
    queries=[]
    def query(method,url,headers,params):queries.append((url,params));return []
    monkeypatch.setattr(intelligence,'_request_json',query)
    intelligence.list_intelligence_queue('district',assigned_to='staff')
    assert len(queries)==2
    assert all(params['district_id']=='eq.district' and params['assigned_to']=='eq.staff' for _,params in queries)


def test_status_hides_other_people_and_their_workloads(member,monkeypatch):
    monkeypatch.setattr(bpp_assignments,'eligible_roster',lambda _:[{'id':member.user_id,'label':'Me'},{'id':'other','label':'Other'}])
    queries=[]
    def query(method,table,**kw):
        queries.append((table,kw))
        if table=='bpp_assignment_settings':return []
        return [{'work_key':'account:P1','assigned_to':member.user_id,'busy':False}]
    monkeypatch.setattr(bpp_assignments,'request',query)
    result=bpp_assignments.status(member)
    assert result['own_assignments_only'] and [r['id'] for r in result['appraisers']]==[member.user_id]
    assert queries[-1][1]['params']['assigned_to']=='eq.'+member.user_id


def test_reservation_status_omits_other_assignments(member,monkeypatch):
    own=make_item(raw={'assigned_to':member.user_id},matched_account_number='P1')
    monkeypatch.setattr(intelligence,'list_intelligence_queue',lambda *a,**kw:[own])
    claims={k:{'work_key':k,'actor_id':'other','actor_email':'appraiser@example.invalid','claimed_at':'today'} for k in ['account:P1','account:P2']}
    monkeypatch.setattr(main.bpp_work_claims,'claims',lambda _:claims)
    result=main.intelligence_work_status(main.ComptrollerStatusRequest(access_token='token'))
    assert set(result['claims'])=={'account:P1'}


def test_historical_scan_requires_current_assignment_or_own_document_item(member,monkeypatch):
    monkeypatch.setattr(intelligence,'get_intelligence_item',lambda *_:make_item(raw={'assigned_to':'prior-appraiser'}))
    query=Mock(return_value=[]);monkeypatch.setattr(bpp_assignments,'request',query)
    assert not visibility.account_evidence_allowed(member,'P123','historic-doc-item')
    query.return_value=[{'id':'assigned-current-work'}]
    assert visibility.account_evidence_allowed(member,'P123','historic-doc-item')
    assert query.call_args.kwargs['params']['assigned_to']=='eq.'+member.user_id
    monkeypatch.setattr(intelligence,'get_intelligence_item',lambda *_:make_item(status='RESOLVED',raw={'assigned_to':member.user_id}))
    query.reset_mock()
    assert visibility.account_evidence_allowed(member,'P123','own-history')
    query.assert_not_called()

@pytest.mark.parametrize('endpoint',[main.returned_mail_detail,main.returned_mail_download,main.returned_mail_research_status])
def test_returned_mail_direct_reads_cannot_bypass_assignment(member,monkeypatch,endpoint):
    from app import returned_mail
    monkeypatch.setattr(returned_mail,'document',lambda *_:{'account_number':'P123','intelligence_item_id':'item'})
    monkeypatch.setattr(visibility,'account_evidence_allowed',lambda *_:False)
    with pytest.raises(HTTPException) as exc:
        endpoint(main.ReturnedMailDocumentRequest(access_token='token',document_id=uuid4()))
    assert exc.value.status_code==403


def test_county_filing_and_batch_source_do_not_bypass_assignments(monkeypatch,member):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.county_clerk import api as county_api,service
    app=FastAPI();app.include_router(county_api.router(lambda _:member))
    client=TestClient(app)
    query=Mock(return_value=[]);monkeypatch.setattr(service,'request',query)
    original=Mock();monkeypatch.setattr(service,'original',original)
    filing_id=str(uuid4())
    assert client.post('/api/county-clerk/filing',json={'access_token':'token','filing_id':filing_id}).status_code==403
    assert query.call_args.args[1]=='bpp_intelligence_items'
    assert query.call_args.kwargs['params']['assigned_to']=='eq.'+member.user_id
    assert client.post('/api/county-clerk/original',json={'access_token':'token','import_id':str(uuid4())}).status_code==403
    original.assert_not_called()
