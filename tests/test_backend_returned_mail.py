from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import Mock
import pytest
from fastapi import HTTPException, BackgroundTasks
from backend import main
from app import returned_mail
from tests.test_backend_intelligence_queue import make_district,make_item

@pytest.fixture
def context(monkeypatch):
    ctx=make_district(role='member')
    from app import bpp_visibility
    monkeypatch.setattr(bpp_visibility,'account_evidence_allowed',lambda *a:True)
    monkeypatch.setattr(main,'require_bpp_access',lambda _:ctx)
    monkeypatch.setattr(main,'get_supabase_user',lambda _: {'id':ctx.user_id})
    monkeypatch.setattr(main.bpp_work_claims,'claim',lambda *a,**kw:None)
    monkeypatch.setattr(main.bpp_work_claims,'release',lambda *a,**kw:None)
    monkeypatch.setattr(main.activity_analytics,'record_activity',lambda *a,**kw:None)
    return ctx

def test_regular_appraiser_can_import_and_district_actor_are_server_attributed(context,monkeypatch):
    importer=Mock(return_value={'duplicate':False});monkeypatch.setattr(returned_mail,'import_scan',importer)
    main.returned_mail_import(main.ReturnedMailUploadRequest(access_token='token',relative_path='P123/a.pdf',file_base64='YWJj'), BackgroundTasks())
    assert importer.call_args.args[0] is context

def test_upload_requires_bpp_access(context,monkeypatch):
    def deny(_):raise HTTPException(403,'No BPP access')
    monkeypatch.setattr(main,'require_bpp_access',deny)
    importer=Mock();monkeypatch.setattr(returned_mail,'import_scan',importer)
    with pytest.raises(HTTPException) as exc:main.returned_mail_import(main.ReturnedMailUploadRequest(access_token='token',relative_path='P123/a.pdf',file_base64='YWJj'), BackgroundTasks())
    assert exc.value.status_code==403;importer.assert_not_called()

def test_list_blocks_foreign_queue_item_before_listing_documents(context,monkeypatch):
    monkeypatch.setattr(main.comptroller_intelligence,'get_intelligence_item',lambda *a:make_item(district_id='other'))
    listing=Mock();monkeypatch.setattr(returned_mail,'documents',listing)
    with pytest.raises(HTTPException) as exc:main.returned_mail_list(main.ReturnedMailListRequest(access_token='token',source_table='bpp_intelligence_items',item_id='item'))
    assert exc.value.status_code==403;listing.assert_not_called()

def test_review_requires_account_reservation(context,monkeypatch):
    monkeypatch.setattr(returned_mail,'document',lambda *a:{'intelligence_item_id':'item'})
    monkeypatch.setattr(main.comptroller_intelligence,'get_intelligence_item',lambda *a:make_item(raw={'assigned_to':context.user_id}))
    def conflict(*a):raise main.bpp_work_claims.WorkConflict('Reserved by another appraiser')
    monkeypatch.setattr(main.bpp_work_claims,'claim',conflict)
    query=Mock();monkeypatch.setattr(returned_mail,'request',query)
    with pytest.raises(HTTPException) as exc:main.returned_mail_review(main.ReturnedMailReviewRequest(access_token='token',document_id=uuid4(),version=0,review_status='REVIEWED',return_reason='UNKNOWN'))
    assert exc.value.status_code==409;query.assert_not_called()

def test_resolve_returned_mail_uses_atomic_rpc(context,monkeypatch):
    original=make_item(item_id='item',raw={'source':'returned_mail_scan','assigned_to':context.user_id},matched_account_number='P123')
    monkeypatch.setattr(main.comptroller_intelligence,'get_intelligence_item',lambda *a:original)
    row={'id':'item','district_id':context.district_id,'source':'returned_mail_scan','status':'RESOLVED','signal_type':'mailing_address_change'}
    query=Mock(return_value=row);monkeypatch.setattr(returned_mail,'request',query)
    generic=Mock();monkeypatch.setattr(main.comptroller_intelligence,'resolve_item',generic)
    result=main.intelligence_resolve(main.IntelligenceResolveRequest(access_token='token',source_table='bpp_intelligence_items',item_id='item',resolution='OTHER'))
    assert query.call_args.args[1]=='rpc/returned_mail_resolve'
    assert query.call_args.kwargs['payload']['p_actor_id']==context.user_id
    assert result['item']['status']=='RESOLVED';generic.assert_not_called()

def test_download_is_authorized_and_not_publicly_cached(context,monkeypatch):
    monkeypatch.setattr(returned_mail,'document',lambda *a:{'storage_path':'private','content_type':'application/pdf','account_number':'P123'})
    monkeypatch.setattr(returned_mail,'storage',lambda *a:SimpleNamespace(content=b'%PDF-test'))
    response=main.returned_mail_download(main.ReturnedMailDocumentRequest(access_token='token',document_id=uuid4()))
    assert response.body==b'%PDF-test' and response.headers['cache-control']=='no-store'

def test_upload_schedules_research_only_after_saved_evidence(context,monkeypatch):
    importer=Mock(return_value={'document_id':'doc','duplicate':True})
    monkeypatch.setattr(returned_mail,'import_scan',importer)
    tasks=BackgroundTasks()
    result=main.returned_mail_import(main.ReturnedMailUploadRequest(access_token='token',relative_path='P123/a.pdf',file_base64='YWJj'),tasks)
    assert result['document_id']=='doc' and len(tasks.tasks)==1
    assert tasks.tasks[0].args==(context.district_id,'doc')


def test_research_read_rejects_foreign_document(context,monkeypatch):
    def reject(*args):raise returned_mail.MailImportError('Not found')
    monkeypatch.setattr(returned_mail,'document',reject)
    query=Mock();monkeypatch.setattr(returned_mail,'request',query)
    with pytest.raises(HTTPException) as exc:
        main.returned_mail_research_status(main.ReturnedMailDocumentRequest(access_token='token',document_id=uuid4()))
    assert exc.value.status_code==404;query.assert_not_called()


def test_internal_worker_requires_service_key_and_only_schedules_work(monkeypatch):
    from starlette.requests import Request
    monkeypatch.setattr(returned_mail,'get_supabase_config',lambda:('https://db.example','service-secret'))
    payload=main.ReturnedMailWorkerRequest(district_id=uuid4())
    tasks=BackgroundTasks()
    for token in ['', 'Bearer user-jwt','Bearer anon-key']:
        req=Request({'type':'http','headers':[(b'authorization',token.encode())]})
        with pytest.raises(HTTPException) as exc:main.returned_mail_retry_worker(payload,req,tasks)
        assert exc.value.status_code==403
    assert not tasks.tasks
    req=Request({'type':'http','headers':[(b'authorization',b'Bearer service-secret')]})
    assert main.returned_mail_retry_worker(payload,req,tasks)=={'accepted':True}
    assert len(tasks.tasks)==1 and tasks.tasks[0].args==(str(payload.district_id),)
