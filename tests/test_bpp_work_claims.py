from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from app import bpp_work_claims as work
from tests.test_backend_intelligence_queue import make_item,make_district
from backend import main


def test_same_account_shared_across_queues_but_unmatched_items_separate():
    assert work.work_key(make_item(matched_account_number=' p001 '))==work.work_key(make_item(item_id='other',signal_type='mailing_address_change',matched_account_number='P001'))
    assert work.work_key(make_item())!=work.work_key(make_item(item_id='other'))


def test_conflicting_owner_never_uses_browser_identity(monkeypatch):
    calls=[]
    def rpc(method,path,**kw):
        calls.append(kw['json_payload'])
        return {'actor_id':'another','actor_email':'other@example.org'}
    monkeypatch.setattr(work,'request',rpc)
    with pytest.raises(work.WorkConflict,match='other@example.org'):work.claim(make_district(),make_item())
    assert calls[0]['p_district_id']=='district-1' and calls[0]['p_actor_id']=='user-1'


def test_api_conflict_prevents_status_change(monkeypatch):
    monkeypatch.setattr(main,'require_bpp_access',lambda token:make_district())
    monkeypatch.setattr(main.comptroller_intelligence,'get_intelligence_item',lambda *a:make_item())
    def conflict(*a):raise work.WorkConflict('Already being worked by another appraiser')
    monkeypatch.setattr(work,'claim',conflict)
    monkeypatch.setattr(main.comptroller_intelligence,'investigate_item',lambda *a,**k:pytest.fail('Must not mutate'))
    with pytest.raises(HTTPException) as exc:main.intelligence_investigate(main.IntelligenceInvestigateRequest(access_token='x',source_table='bpp_intelligence_items',item_id='intel-1'))
    assert exc.value.status_code==409


def test_release_is_owner_scoped_except_for_admin(monkeypatch):
    calls=[]
    monkeypatch.setattr(work,'claims',lambda *a:{})
    monkeypatch.setattr(work,'request',lambda *a,**kw:calls.append(kw['params']))
    work.release(make_district(role='appraiser'),make_item(),admin=True)
    assert calls[-1]['actor_id']=='eq.user-1' and calls[-1]['district_id']=='eq.district-1'
    work.release(make_district(role='admin'),make_item(),admin=True)
    assert 'actor_id' not in calls[-1]


def test_claim_alias_survives_account_match_change(monkeypatch):
    row={'actor_id':'user-1','actor_email':'me@example.org','claimed_at':'2026-10-28','work_key':'item:bpp_intelligence_items:intel-1','source_table':'bpp_intelligence_items','item_id':'intel-1'}
    monkeypatch.setattr(work,'request',lambda *a,**kw:[row])
    item=make_item(matched_account_number='P123')
    from dataclasses import asdict
    result=work.attach([asdict(item)],make_district())
    assert result[0]['work_claim']['mine']
    assert 'actor_id' not in result[0]['work_claim']


def test_complete_releases_claim_only_after_success(monkeypatch):
    calls=[]
    monkeypatch.setattr(main,'require_bpp_access',lambda token:make_district())
    monkeypatch.setattr(main,'get_supabase_user',lambda token:{'id':'user-1'})
    monkeypatch.setattr(main.comptroller_intelligence,'get_intelligence_item',lambda *a:make_item())
    monkeypatch.setattr(work,'claim',lambda *a:calls.append('claim'))
    monkeypatch.setattr(work,'release',lambda *a:calls.append('release'))
    monkeypatch.setattr(main.activity_analytics,'record_activity',lambda *a,**k:None)
    def resolved(*a,**kw):calls.append('resolve');return make_item(status='RESOLVED')
    monkeypatch.setattr(main.comptroller_intelligence,'resolve_item',resolved)
    main.intelligence_resolve(main.IntelligenceResolveRequest(access_token='x',source_table='bpp_intelligence_items',item_id='intel-1',resolution='EXISTING_ACCOUNT'))
    assert calls==['claim','resolve','release']


def test_release_rejects_another_appraiser(monkeypatch):
    item=make_item()
    monkeypatch.setattr(work,'claims',lambda *a:{work.work_key(item):{'actor_id':'other'}})
    monkeypatch.setattr(work,'request',lambda *a,**k:pytest.fail('Must not release'))
    with pytest.raises(work.WorkConflict):work.release(make_district(role='member'),item,admin=True)


@pytest.mark.parametrize('action,request_type,extra',[
    ('intelligence_resolve','IntelligenceResolveRequest',{'resolution':'EXISTING_ACCOUNT'}),
    ('intelligence_dismiss','IntelligenceDismissRequest',{}),
])
def test_conflict_also_blocks_completion_and_dismissal(monkeypatch,action,request_type,extra):
    monkeypatch.setattr(main,'require_bpp_access',lambda token:make_district())
    monkeypatch.setattr(main.comptroller_intelligence,'get_intelligence_item',lambda *a:make_item())
    def conflict(*a):raise work.WorkConflict('Another appraiser owns it')
    monkeypatch.setattr(work,'claim',conflict)
    monkeypatch.setattr(main.comptroller_intelligence,'resolve_item',lambda *a,**k:pytest.fail('Must not mutate'))
    monkeypatch.setattr(main.comptroller_intelligence,'dismiss_item',lambda *a,**k:pytest.fail('Must not mutate'))
    request=getattr(main,request_type)(access_token='x',source_table='bpp_intelligence_items',item_id='intel-1',**extra)
    with pytest.raises(HTTPException) as exc:getattr(main,action)(request)
    assert exc.value.status_code==409


def test_cross_district_release_blocked_before_claim_lookup(monkeypatch):
    monkeypatch.setattr(main,'require_bpp_access',lambda token:make_district())
    monkeypatch.setattr(main.comptroller_intelligence,'get_intelligence_item',lambda *a:make_item(district_id='other'))
    monkeypatch.setattr(work,'release',lambda *a,**k:pytest.fail('Must not release'))
    with pytest.raises(HTTPException) as exc:main.intelligence_release(main.IntelligenceItemRequest(access_token='x',source_table='bpp_intelligence_items',item_id='intel-1'))
    assert exc.value.status_code==403


def test_stale_tab_cannot_restart_completed_review(monkeypatch):
    monkeypatch.setattr(work,'claim',lambda *a:pytest.fail('Must not claim completed work'))
    with pytest.raises(HTTPException) as exc:main._claim_bpp_work(make_district(),make_item(status='RESOLVED'))
    assert exc.value.status_code==409
