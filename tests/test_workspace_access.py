import json
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from backend import main
from app import workspace_access as access

@pytest.fixture
def grants(monkeypatch):
    objects={}
    class Response:
        def __init__(self,data=None):self.status_code=200 if data is not None else 404;self.data=data
        def json(self):return json.loads(self.data) if self.data else {}
        def raise_for_status(self):pass
    def request(method,district,name,data=None):
        if method=='POST':objects[(district,name)]=data
        return Response(objects.get((district,name)))
    monkeypatch.setattr(access,'object_request',request)
    return objects

def district(email='member@cad.org',role='member',id='d1'):
    return SimpleNamespace(district_id=id,email=email,role=role,district_slug='test',district_name='Test CAD',domain='cad.org',user_id=email)

def test_permissions_default_preserve_existing_and_persist(grants):
    assert access.access_for(district())=={'bpp':True,'commercial':True}
    access.save_access('d1','MEMBER@cad.org',False,True,'admin@cad.org')
    assert access.access_for(district())=={'bpp':False,'commercial':True}
    with pytest.raises(HTTPException) as denied:access.require_access(district(),'bpp')
    assert denied.value.status_code==403
    access.require_access(district(),'commercial')
    assert access.access_for(district(id='d2'))=={'bpp':True,'commercial':True}
    assert access.access_for(district(role='admin'))=={'bpp':True,'commercial':True}
    with pytest.raises(HTTPException):access.save_access('d1','member@cad.org',False,False,'admin')

@pytest.fixture
def api(monkeypatch,grants):
    def context(token):
        if token=='admin':return district('admin@cad.org','admin')
        if token in ('bpp','commercial','both'):return district(token+'@cad.org')
        raise HTTPException(401,'Sign in required')
    monkeypatch.setattr(main,'get_authenticated_district_context',context)
    monkeypatch.setattr(main,'get_supabase_service_role_key',lambda:'test-only')
    monkeypatch.setattr(main,'get_supabase_config',lambda:('https://test.invalid','test-only'))
    users=[{'email':name+'@cad.org','role':'member','user_id':name} for name in ('bpp','commercial','both')]+[{'email':'admin@cad.org','role':'admin','user_id':'admin'}]
    monkeypatch.setattr(main,'list_district_users',lambda **kw:users)
    access.save_access('d1','bpp@cad.org',True,False,'admin')
    access.save_access('d1','commercial@cad.org',False,True,'admin')
    return TestClient(main.app),users

def test_server_rejects_wrong_workspace_before_bpp_execution(api):
    client,_=api
    assert client.post('/api/calculate',json={}).status_code==401
    assert client.post('/api/calculate',headers={'Authorization':'Bearer commercial'},json={}).status_code==403
    # Valid BPP access reaches schema validation, rather than permission denial.
    assert client.post('/api/review/run',headers={'Authorization':'Bearer bpp'},json={}).status_code==422
    assert client.post('/api/review/run',json={'access_token':'bpp'}).status_code==422
    with pytest.raises(HTTPException):main.get_commercial_context('bpp')
    assert main.get_commercial_context('commercial').email=='commercial@cad.org'

def test_admin_updates_only_own_members_and_grants_apply_immediately(api):
    client,_=api
    payload={'access_token':'admin','email':'bpp@cad.org','bpp_access':False,'commercial_access':True}
    response=client.post('/api/district/users/access',json=payload)
    assert response.status_code==200
    assert main.get_commercial_context('bpp').email=='bpp@cad.org'
    assert client.post('/api/calculate',headers={'Authorization':'Bearer bpp'},json={}).status_code==403
    assert client.post('/api/district/users/access',json={**payload,'access_token':'commercial'}).status_code==403
    assert client.post('/api/district/users/access',json={**payload,'email':'outsider@cad.org'}).status_code==404
    assert client.post('/api/district/users/access',json={**payload,'email':'admin@cad.org'}).status_code==400

def test_invitation_keeps_selected_access_before_account_creation(api,monkeypatch):
    client,users=api
    monkeypatch.setattr(main,"admin_auth_request",lambda *a:{"id":"new-user"})
    monkeypatch.setattr(main,"get_invited_district_user",lambda **kw:None)
    def link(**kw):
        assert access.read_access('d1',kw['email'])=={'bpp':False,'commercial':True}
        users.append({'email':kw['email'],'role':'member','user_id':None})
    monkeypatch.setattr(main,'link_user_to_district',link)
    response=client.post('/api/district/users/invite',json={'access_token':'admin','email':'new@cad.org','role':'member','bpp_access':False,'commercial_access':True})
    assert response.status_code==200
    assert response.json()['users'][-1]['workspace_access']=={'bpp':False,'commercial':True}



def test_permission_storage_outage_fails_closed(monkeypatch):
    import requests
    def unavailable(*args):raise requests.ConnectionError('offline')
    monkeypatch.setattr(access,'object_request',unavailable)
    with pytest.raises(HTTPException) as denied:access.require_access(district(),'commercial')
    assert denied.value.status_code==503


def test_bpp_member_can_use_intelligence_but_cannot_use_admin_or_readiness(api,monkeypatch):
    client,_=api
    seen=[]
    monkeypatch.setattr(main.comptroller_intelligence,'get_queue_summary',lambda district_id:seen.append(district_id) or {'new':0})
    monkeypatch.setattr(main.comptroller_intelligence,'list_intelligence_queue',lambda district_id,**kw:[])
    assert client.post('/api/admin/intelligence/summary',json={'access_token':'bpp'}).status_code==200
    assert client.post('/api/admin/intelligence/queue',json={'access_token':'bpp','signal_type':'mailing_address_change'}).status_code==200
    assert seen==['d1']
    assert client.post('/api/admin/intelligence/queue',json={'access_token':'commercial'}).status_code==403
    assert client.post('/api/admin/production-readiness',json={'access_token':'bpp'}).status_code==403
    assert client.post('/api/district/users',json={'access_token':'bpp'}).status_code==403
    assert client.post('/api/district/users/invite',json={'access_token':'bpp','email':'x@cad.org'}).status_code==403
    assert client.post('/api/district/users/remove',json={'access_token':'bpp','membership_id':'target'}).status_code==403
    access.save_access('d1','bpp@cad.org',False,True,'admin')
    assert client.post('/api/admin/intelligence/summary',json={'access_token':'bpp'}).status_code==403


def test_bpp_member_review_actions_keep_district_isolation(api,monkeypatch):
    from tests.test_backend_intelligence_queue import make_item
    client,_=api
    other=make_item(district_id='other-district')
    monkeypatch.setattr(main.comptroller_intelligence,'get_intelligence_item',lambda *a:other)
    mutate=__import__('unittest.mock',fromlist=['Mock']).Mock()
    monkeypatch.setattr(main.comptroller_intelligence,'dismiss_item',mutate)
    response=client.post('/api/admin/intelligence/dismiss',json={'access_token':'bpp','source_table':'bpp_intelligence_items','item_id':'other'})
    assert response.status_code==403
    mutate.assert_not_called()


def test_bpp_member_can_review_own_district_item(api,monkeypatch):
    from tests.test_backend_intelligence_queue import make_item
    from unittest.mock import Mock
    client,_=api
    own=make_item(district_id='d1')
    monkeypatch.setattr(main.comptroller_intelligence,'get_intelligence_item',lambda *a:own)
    monkeypatch.setattr(main,'get_supabase_user',lambda _:{'id':'bpp-user'})
    mutate=Mock(return_value=own)
    monkeypatch.setattr(main.comptroller_intelligence,'dismiss_item',mutate)
    response=client.post('/api/admin/intelligence/dismiss',json={'access_token':'bpp','source_table':'bpp_intelligence_items','item_id':own.id})
    assert response.status_code==200
    assert mutate.call_args.kwargs['reviewed_by']=='bpp-user'
    monkeypatch.setattr(main.comptroller_jurisdictions,'get_jurisdiction_by_district_id',lambda _:None)
    assert client.post('/api/admin/intelligence/cad-health',json={'access_token':'bpp'}).status_code==200
    assert client.post('/api/admin/property/lookup',json={'access_token':'commercial','address':'100 Main St'}).status_code==403


@pytest.fixture(autouse=True)
def isolated_work_claims(monkeypatch):
    monkeypatch.setattr(main.bpp_work_claims,'claim',lambda *a:None)
    monkeypatch.setattr(main.bpp_work_claims,'claims',lambda *a:{})
    monkeypatch.setattr(main.bpp_work_claims,'release',lambda *a,**kw:None)


def test_district_policy_restricts_admin_members_and_new_grants(grants):
    grants[('d1','workspace-policy.json')]=json.dumps({'bpp':True,'commercial':False}).encode()
    for role in ('admin','appraiser','member'):
        d=district(role=role)
        assert access.access_for(d)=={'bpp':True,'commercial':False}
        with pytest.raises(HTTPException) as denied:access.require_access(d,'commercial')
        assert denied.value.status_code==403
        access.require_access(d,'bpp')
    access.save_access('d1','new@cad.org',True,True,'admin')
    assert access.read_access('d1','new@cad.org')=={'bpp':True,'commercial':False}
    assert access.access_for(district(id='other'))=={'bpp':True,'commercial':True}


def test_bpp_only_district_admin_retains_admin_but_cannot_enter_commercial(api,grants):
    client,_=api
    grants[('d1','workspace-policy.json')]=json.dumps({'bpp':True,'commercial':False}).encode()
    assert main.require_district_admin('admin').role=='admin'
    main.require_bpp_access('admin')
    with pytest.raises(HTTPException) as denied:main.get_commercial_context('admin')
    assert denied.value.status_code==403
    users=client.post('/api/district/users',json={'access_token':'admin'})
    assert users.status_code==200
    admin=next(u for u in users.json()['users'] if u['role']=='admin')
    assert admin['workspace_access']=={'bpp':True,'commercial':False}


@pytest.mark.parametrize('role',['member','appraiser'])
def test_regular_bpp_appraiser_can_complete_and_print_account_card_flow(api,monkeypatch,role):
    """Exercise real HTTP authorization; mock storage/enrichment, never BPP guards."""
    from dataclasses import replace
    from tests.test_new_account_enrichment import make_item,make_jurisdiction
    client,_=api
    context=district('bpp@cad.org',role)
    original_context=main.get_authenticated_district_context
    monkeypatch.setattr(main,'get_authenticated_district_context',lambda token:context if token=='bpp' else original_context(token))
    item_id='00000000-0000-0000-0000-000000000123'
    current=[make_item(id=item_id,district_id='d1')]
    printed=[]
    monkeypatch.setattr(main.comptroller_intelligence,'get_intelligence_item',lambda *a:current[0])
    monkeypatch.setattr(main.comptroller_jurisdictions,'get_jurisdiction',lambda _:make_jurisdiction(district_id='d1'))
    monkeypatch.setattr(main.comptroller_new_account_enrichment,'generate_account_card',lambda *a:object())
    monkeypatch.setattr(main.comptroller_new_account_enrichment,'account_card_to_dict',lambda _:dict(appraiser={},exceptions=[],personal_property_accounts=[]))
    monkeypatch.setattr(main,'get_supabase_user',lambda _:dict(id=context.user_id))
    monkeypatch.setattr(main.activity_analytics,'record_activity',lambda *a,**kw:None)
    def claim(ctx,item):
        assert ctx.role==role and ctx.user_id==context.user_id
    monkeypatch.setattr(main.bpp_work_claims,'claim',claim)
    def investigate(*args,**kw):
        current[0]=replace(current[0],status='IN_REVIEW');return current[0]
    def resolve(*args,**kw):
        assert kw['reviewed_by']==context.user_id
        current[0]=replace(current[0],status='RESOLVED',resolution=kw['resolution']);return current[0]
    monkeypatch.setattr(main.comptroller_intelligence,'investigate_item',investigate)
    monkeypatch.setattr(main.comptroller_intelligence,'resolve_item',resolve)
    def storage(method,table,params=None,payload=None):
        if table=='rpc/bpp_save_account_card':
            assert payload['p_district_id']=='d1' and payload['p_actor_id']==context.user_id
            current[0]=replace(current[0],raw={'account_card_draft':payload['p_data'],'account_card_version':1})
            return {'version':1,'saved_at':'2026-10-29T01:00:00Z'}
        if table=='bpp_intelligence_items':
            assert params['district_id']=='eq.d1' and params['reviewed_by']=='eq.'+context.user_id
            assert current[0].status=='RESOLVED'
            return [{'id':item_id,'account_card_draft':current[0].raw['account_card_draft'],'account_card_version':1,'bpp_account_card_prints':printed}]
        assert table=='rpc/bpp_confirm_account_card_prints'
        assert payload['p_district_id']=='d1' and payload['p_actor_id']==context.user_id
        printed[:]=[{'item_id':item_id,'version':1,'printed_at':'2026-10-29T01:01:00Z'}]
        return printed
    monkeypatch.setattr(main.comptroller_cad_review,'_request',storage)
    base={'access_token':'bpp','source_table':'bpp_intelligence_items','item_id':item_id}
    assert client.post('/api/admin/intelligence/investigate',json=base).status_code==200
    card=client.post('/api/admin/intelligence/account-card',json=base)
    assert card.status_code==200
    draft=card.json()['card']['data'];draft['business_name']='Appraiser test';draft['segments'][0]['flat_value']='123.45'
    saved=client.post('/api/admin/intelligence/account-card/save',json={**base,'version':0,'data':draft})
    assert saved.status_code==200 and saved.json()['total_value']=='123.45'
    reopened=client.post('/api/admin/intelligence/account-card',json=base)
    assert reopened.json()['card']['data']['business_name']=='Appraiser test'
    assert client.post('/api/admin/intelligence/resolve',json={**base,'resolution':'NEW_ACCOUNT_NEEDED'}).status_code==200
    cards=client.post('/api/admin/intelligence/account-cards/completed',json={'access_token':'bpp'})
    assert cards.status_code==200 and cards.json()['cards'][0]['printed_at'] is None
    assert cards.json()['cards'][0]['data']['business_name']=='Appraiser test'
    result=client.post('/api/admin/intelligence/account-cards/confirm-printed',json={'access_token':'bpp','cards':[{'id':item_id,'version':1}]})
    assert result.status_code==200
    reloaded=client.post('/api/admin/intelligence/account-cards/completed',json={'access_token':'bpp'})
    assert reloaded.json()['cards'][0]['printed_at']=='2026-10-29T01:01:00Z'
    # Admin-only tools remain unavailable despite full BPP workflow access.
    assert client.post('/api/district/users',json={'access_token':'bpp'}).status_code==403
    assert client.post('/api/admin/production-readiness',json={'access_token':'bpp'}).status_code==403
    # Every card endpoint rejects a user without BPP access before storage runs.
    for path,body in [('/account-card',base),('/account-card/save',{**base,'version':1,'data':draft}),('/account-cards/completed',{}),('/account-cards/confirm-printed',{'cards':[{'id':item_id,'version':1}]}),('/resolve',{**base,'resolution':'NEW_ACCOUNT_NEEDED'})]:
        assert client.post('/api/admin/intelligence'+path,json={**body,'access_token':'commercial'}).status_code==403
