from datetime import datetime,timezone
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from app import super_admin as sa
D='7f219b49-3da0-41c4-801f-7b2867810286'

@pytest.fixture
def client(monkeypatch):
    users={'owner':{'id':'owner','app_metadata':{sa.CLAIM:True}},'admin':{'id':'county-admin','app_metadata':{'role':'admin'}},'spoof':{'user_metadata':{sa.CLAIM:True}},'string':{'app_metadata':{sa.CLAIM:'true'}},'temporary':{'app_metadata':{sa.CLAIM:True,'must_change_password':True}}}
    calls=[]
    def read(table,**params):
        calls.append((table,params))
        return {'districts':[{'id':D,'name':'Dallam CAD','county':'Dallam','domain':'dallamcad.org','active':True}],
         'district_users':[{'email':'holly@example.test','full_name':'Holly','role':'admin','active':True,'user_id':'h'},{'email':'morgan@example.test','role':'appraiser','active':True,'user_id':'m'}],
         'jurisdictions':[{'id':'j','active':True,'county_name':'Dallam','comptroller_county_code':'56','capabilities':{},'property_field_mapping':{}}],
         'comptroller_sync_runs':[{'status':'SUCCESS','finished_at':datetime.now(timezone.utc).isoformat()}],
         'property_source_imports':[], 'bpp_intelligence_items':[]}[table]
    monkeypatch.setattr(sa,'read',read)
    monkeypatch.setattr(sa,'district_access',lambda _:{'bpp':True,'commercial':False})
    monkeypatch.setattr(sa,'access_for',lambda _:{'bpp':True,'commercial':False})
    monkeypatch.setattr(sa,'login_status',lambda _:{'state':'Temporary password — first login required','last_sign_in_at':None})
    monkeypatch.setattr(sa,'count',lambda table,**kw:454 if table=='comptroller_permit_locations' else 0)
    monkeypatch.setattr(sa,'get_supabase_config',lambda:('https://db.test','key'))
    monkeypatch.setattr(sa,'_paginated_get',lambda *a,**k:read('districts'))
    app=FastAPI();app.include_router(sa.router(lambda token:users[token]));return TestClient(app),calls

@pytest.mark.parametrize('token',['admin','spoof','string','temporary'])
def test_county_admin_or_client_edit_cannot_read_other_districts(client,token):
    c,calls=client
    assert c.post('/api/super-admin/districts',json={'access_token':token}).status_code==403
    assert c.post('/api/super-admin/review',json={'access_token':token,'district_id':D}).status_code==403
    assert not calls

def test_owner_can_review_dallam_without_changing_tenant_identity(client):
    c,calls=client
    assert c.post('/api/super-admin/districts',json={'access_token':'owner'}).status_code==200
    r=c.post('/api/super-admin/review',json={'access_token':'owner','district_id':D})
    assert r.status_code==200,r.text
    data=r.json();assert not data['ready']
    assert data['district']['id']==D and data['permit_count']==454
    assert data['users'][0]['role']=='admin'
    assert 'user_id' not in data['users'][0]
    assert not next(x for x in data['checks'] if x['label']=='CAD account export')['passed']
    assert next(x for x in data['checks'] if x['label']=='Sales-tax permit sync')['passed']
    for table,params in calls:
        if table in ('district_users','bpp_intelligence_items'):assert params['district_id']=='eq.'+D
        if table=='property_source_imports':assert params['jurisdiction_id']=='eq.j'

def test_invalid_district_and_mutating_routes_are_not_supported(client):
    c,_=client
    assert c.post('/api/super-admin/review',json={'access_token':'owner','district_id':'not-a-uuid'}).status_code==422
    assert c.post('/api/super-admin/users/remove',json={'access_token':'owner'}).status_code==404
