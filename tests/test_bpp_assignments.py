from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from fastapi import HTTPException
from app import bpp_assignments as assign
from backend import main


def test_roster_includes_admins_and_bpp_members_not_invites_or_commercial_only(monkeypatch):
    rows=[{'user_id':'admin','email':'admin@example.invalid','role':'admin'},
          {'user_id':'bpp','email':'bpp@example.invalid','role':'appraiser'},
          {'user_id':'commercial','email':'commercial@example.invalid','role':'appraiser'},
          {'user_id':None,'email':'invite@example.invalid','role':'appraiser'}]
    monkeypatch.setattr(assign,'request',lambda *a,**k:rows)
    monkeypatch.setattr(assign,'district_access',lambda _:{'bpp':True})
    monkeypatch.setattr(assign,'read_access',lambda d,email:{'bpp':email.startswith('bpp')})
    assert {r['id'] for r in assign.eligible_roster('district')}=={'admin','bpp'}


def test_configure_uses_server_district_and_actor(monkeypatch):
    ctx=SimpleNamespace(district_id='district',user_id='admin')
    monkeypatch.setattr(assign,'eligible_roster',lambda _:[{'id':'member'}])
    request=Mock(return_value=5);monkeypatch.setattr(assign,'request',request)
    assert assign.configure(ctx,True)['allocated_items']==5
    assert request.call_args.kwargs['payload']=={'p_district_id':'district','p_actor_id':'admin','p_enabled':True,'p_roster':[{'id':'member'}]}


def test_workload_counts_unique_accounts_across_signals(monkeypatch):
    monkeypatch.setattr(assign,'eligible_roster',lambda _:[{'id':'a','label':'A','email':'a@example.invalid'}])
    monkeypatch.setattr(assign,'request',lambda method,table,**kw:[] if table=='bpp_assignment_settings' else [{'work_key':'account:P1','assigned_to':'a','busy':False},{'work_key':'account:P1','assigned_to':'a','busy':True},{'work_key':'account:P2','assigned_to':None,'busy':False}])
    result=assign.status(SimpleNamespace(district_id='district',role='admin',user_id='admin'))
    assert result['appraisers'][0]['open_accounts']==1
    assert result['unassigned_accounts']==1


def test_configure_endpoint_does_not_allow_regular_appraisers(monkeypatch):
    def deny(_):raise HTTPException(403,'Admin required')
    monkeypatch.setattr(main,'require_district_admin',deny)
    configure=Mock();monkeypatch.setattr(assign,'configure',configure)
    with pytest.raises(HTTPException) as exc:main.intelligence_assignment_configure(main.AssignmentConfigureRequest(access_token='member',enabled=True))
    assert exc.value.status_code==403;configure.assert_not_called()


def test_reassign_rejects_outside_roster_before_rpc(monkeypatch):
    monkeypatch.setattr(assign,'eligible_roster',lambda _:[{'id':'local'}])
    rpc=Mock();monkeypatch.setattr(assign,'request',rpc)
    with pytest.raises(ValueError):assign.reassign(SimpleNamespace(district_id='district'),SimpleNamespace(),'foreign')
    rpc.assert_not_called()


def test_my_work_filter_uses_authenticated_user_id(monkeypatch):
    from tests.test_backend_intelligence_queue import make_district
    ctx=make_district(role='member')
    monkeypatch.setattr(main,'require_bpp_access',lambda _:ctx)
    listing=Mock(return_value=[]);monkeypatch.setattr(main.comptroller_intelligence,'list_intelligence_queue',listing)
    monkeypatch.setattr(main.comptroller_intelligence,'reviewer_labels',lambda *a:{})
    monkeypatch.setattr(main.bpp_work_claims,'attach',lambda rows,context:rows)
    main.intelligence_queue(main.IntelligenceQueueRequest(access_token='token',assigned_to='ME'))
    assert listing.call_args.kwargs['assigned_to']==ctx.user_id
