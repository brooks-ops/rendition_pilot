from dataclasses import replace
from unittest.mock import Mock
import pytest
from fastapi import HTTPException
from backend import main as backend
from tests.test_backend_intelligence_queue import make_district

@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setattr(backend,'get_supabase_config',lambda:('https://db.test','anon'))
    monkeypatch.setattr(backend,'get_supabase_service_role_key',lambda:'service')
    monkeypatch.setattr(backend,'require_district_admin',lambda _:make_district())
    monkeypatch.setattr(backend,'get_invited_district_user',lambda **k:None)
    monkeypatch.setattr(backend,'list_district_users',lambda **k:[])
    create=Mock(return_value={'id':'new-user'})
    link=Mock()
    monkeypatch.setattr(backend,'admin_auth_request',create)
    monkeypatch.setattr(backend,'link_user_to_district',link)
    return create,link

def test_create_generates_confirmed_login_with_forced_change(setup):
    create,link=setup
    result=backend.district_user_invite(backend.DistrictInviteRequest(access_token='token',email=' New@cad.org '))
    payload=create.call_args.args[2]
    assert payload['email_confirm'] is True
    assert payload['app_metadata']['must_change_password'] is True
    assert payload['password']==result['temporary_password']
    assert len(result['temporary_password'])>=24
    assert link.call_args.kwargs['user_id']=='new-user'
    assert link.call_args.kwargs['email']=='new@cad.org'

def test_existing_account_password_is_unchanged(setup,monkeypatch):
    monkeypatch.setattr(backend,'get_invited_district_user',lambda **k:replace(make_district(),user_id='existing'))
    result=backend.district_user_invite(backend.DistrictInviteRequest(access_token='t',email='admin@cad.org'))
    assert result['already_authorized']
    setup[0].assert_not_called();setup[1].assert_not_called()

def test_crossdistrict_create_is_denied(setup,monkeypatch):
    monkeypatch.setattr(backend,'get_invited_district_user',lambda **k:make_district(district_id='other'))
    with pytest.raises(HTTPException):backend.district_user_invite(backend.DistrictInviteRequest(access_token='t',email='x@cad.org'))
    setup[0].assert_not_called()

def test_forced_change_blocks_protected_access(monkeypatch):
    monkeypatch.setattr(backend,'get_supabase_user',lambda _: {})
    monkeypatch.setattr(backend,'build_district_context',lambda *a:replace(make_district(),must_change_password=True))
    with pytest.raises(HTTPException) as exc:backend.get_authenticated_district_context('t')
    assert exc.value.status_code==403

def test_removed_membership_blocks_access(monkeypatch):
    monkeypatch.setattr(backend,'get_supabase_user',lambda _: {})
    monkeypatch.setattr(backend,'build_district_context',lambda *a:None)
    with pytest.raises(HTTPException):backend.get_authenticated_district_context('t')

def test_password_change_authorizes_before_mutation(setup,monkeypatch):
    def deny(*a):raise HTTPException(status_code=403)
    monkeypatch.setattr(backend,'auth_restore',deny)
    put=Mock();monkeypatch.setattr(backend.requests,'put',put)
    with pytest.raises(HTTPException):backend.auth_accept_invitation(backend.AcceptInvitationRequest(access_token='t',password='new-password',confirm_password='new-password'))
    put.assert_not_called();setup[0].assert_not_called()

def test_failed_password_change_keeps_forced_flag(setup,monkeypatch):
    monkeypatch.setattr(backend,'auth_restore',lambda *a:{})
    monkeypatch.setattr(backend.requests,'put',lambda *a,**k:Mock(status_code=422))
    with pytest.raises(HTTPException):backend.auth_accept_invitation(backend.AcceptInvitationRequest(access_token='t',password='new-password',confirm_password='new-password'))
    setup[0].assert_not_called()

def test_remove_uses_verified_actor(setup,monkeypatch):
    monkeypatch.setattr(backend,'get_supabase_user',lambda _:{'id':'actor','email':'admin@cad.org'})
    remove=Mock(return_value={'removed':True});monkeypatch.setattr(backend,'remove_district_user',remove)
    backend.district_user_remove(backend.DistrictUserActionRequest(access_token='t',membership_id='target'))
    assert remove.call_args.kwargs['actor_user_id']=='actor'
    assert remove.call_args.kwargs['district_id']=='district-1'

def test_successful_password_change_clears_server_owned_flag(setup,monkeypatch):
    monkeypatch.setattr(backend,'auth_restore',lambda *a:{'district':{'must_change_password':False}})
    monkeypatch.setattr(backend.requests,'put',lambda *a,**k:Mock(status_code=200))
    monkeypatch.setattr(backend,'get_supabase_user',lambda _:{'id':'new-user','app_metadata':{'must_change_password':True,'other':'retained'}})
    result=backend.auth_accept_invitation(backend.AcceptInvitationRequest(access_token='t',password='new-password',confirm_password='new-password'))
    assert result['district']['must_change_password'] is False
    assert setup[0].call_args.args[2]['app_metadata']=={'must_change_password':False,'other':'retained'}

# Synthetic counties: exercise the same implementation without adding live tenants.
@pytest.mark.parametrize('slug,name,domain',[
    ('north-cad','North County CAD','northcad.example'),
    ('coastal-cad','Coastal County Appraisal District','coastal.example'),
    ('hill-cad','Hill County CAD','hill.example'),
])
def test_any_county_uses_signed_in_admin_district(setup,monkeypatch,slug,name,domain):
    district=replace(make_district(),district_id=f'{slug}-id',district_slug=slug,
        district_name=name,email=f'admin@{domain}',user_id=f'{slug}-admin',domain=domain)
    monkeypatch.setattr(backend,'require_district_admin',lambda _:district)
    monkeypatch.setattr(backend,'get_supabase_user',lambda _:{'id':district.user_id,'email':district.email})
    listed=Mock(return_value=[])
    monkeypatch.setattr(backend,'list_district_users',listed)
    grants=Mock()
    if hasattr(backend,'save_access'):monkeypatch.setattr(backend,'save_access',grants)
    email=f'appraiser@{domain}'
    result=backend.district_user_invite(backend.DistrictInviteRequest(access_token='token',email=email))
    assert result['temporary_password']
    assert setup[1].call_args.kwargs['district_id']==district.district_id
    assert setup[1].call_args.kwargs['email']==email
    assert listed.call_args.kwargs['district_id']==district.district_id
    if hasattr(backend,'save_access'):assert grants.call_args.args[0]==district.district_id
    # A supplied district ID cannot override the actor's verified district.
    remove=Mock(return_value={'removed':True})
    monkeypatch.setattr(backend,'remove_district_user',remove)
    backend.district_user_remove(backend.DistrictUserActionRequest(access_token='token',membership_id='target',district_id='another-county'))
    assert remove.call_args.kwargs['district_id']==district.district_id
    assert remove.call_args.kwargs['actor_user_id']==district.user_id
    # Existing membership in any other county is rejected before Auth mutation.
    setup[0].reset_mock()
    monkeypatch.setattr(backend,'get_invited_district_user',lambda **kw:replace(district,district_id='another-county'))
    with pytest.raises(HTTPException):
        backend.district_user_invite(backend.DistrictInviteRequest(access_token='token',email=email))
    setup[0].assert_not_called()

@pytest.mark.parametrize('district_id',['north-cad-id','coastal-cad-id','hill-cad-id'])
def test_first_login_gate_follows_account_for_any_county(setup,monkeypatch,district_id):
    district=replace(make_district(),district_id=district_id)
    monkeypatch.setattr(backend,'resolve_district_for_user',lambda **kw:district)
    user={'id':'new-user','email':'new@county.example','app_metadata':{'must_change_password':True}}
    monkeypatch.setattr(backend,'get_supabase_user',lambda _:user)
    restored=backend.auth_restore(backend.SessionRequest(access_token='t'))
    assert restored['district']['district_id']==district_id
    assert restored['district']['must_change_password'] is True
    with pytest.raises(HTTPException) as denied:backend.get_authenticated_district_context('t')
    assert denied.value.status_code==403
    user['app_metadata']['must_change_password']=False
    assert backend.get_authenticated_district_context('t').district_id==district_id
