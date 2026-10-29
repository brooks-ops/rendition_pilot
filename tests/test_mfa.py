import json
import pytest
import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app import mfa
from tests.test_backend_intelligence_queue import make_district
from tests.test_security_hardening import token

FACTOR='f95fd4ca-0368-4ec7-aa0f-aed909dcd819'

@pytest.fixture
def client(monkeypatch):
    current={'id':'u','email':'admin@example.invalid','factors':[{'id':FACTOR,'factor_type':'totp','status':'verified'}]}
    calls=[]
    def provider(method,url,**kw):
        calls.append((method,url,kw))
        if url.endswith('/challenge'):data={'id':'challenge'}
        elif url.endswith('/verify'):data={'access_token':token(aal='aal2'),'refresh_token':'rotated','expires_in':3600}
        elif method=='DELETE':data={}
        else:data={'id':FACTOR,'totp':{'secret':'test-only','qr_code':'<svg/>'}}
        return type('Result',(),{'status_code':200,'content':b'json','json':lambda _:data})()
    monkeypatch.setattr(mfa.requests,'request',provider)
    app=FastAPI();app.include_router(mfa.router(lambda _:current,lambda *a:make_district(role='admin'),lambda:('https://auth.test','anon'),lambda d:{'district_id':d.district_id}))
    return TestClient(app),current,calls


def test_verified_factor_requires_code_before_privileged_session(client):
    c,user,calls=client
    assert c.post('/api/auth/mfa/status',json={'access_token':token()}).json()['required']
    r=c.post('/api/auth/mfa/verify',json={'access_token':token(),'factor_id':FACTOR,'code':'123456'})
    assert r.status_code==200 and r.json()['mfa_required'] is False
    assert calls[-1][2]['json']=={'challenge_id':'challenge','code':'123456'}


def test_factor_owned_by_another_user_cannot_be_verified(client):
    c,user,calls=client
    r=c.post('/api/auth/mfa/verify',json={'access_token':token(),'factor_id':'d95fd4ca-0368-4ec7-aa0f-aed909dcd819','code':'123456'})
    assert r.status_code==403 and not calls


def test_enrollment_never_deletes_verified_factor(client):
    c,user,calls=client
    assert c.post('/api/auth/mfa/enroll',json={'access_token':token()}).status_code==409
    assert not calls
    user['factors'][0]['status']='unverified'
    assert c.post('/api/auth/mfa/enroll',json={'access_token':token()}).status_code==200
    assert [call[0] for call in calls]==['DELETE','POST']


def test_mfa_outage_is_retryable_without_changing_account(client,monkeypatch):
    c,user,calls=client
    def unavailable(*a,**kw):raise requests.ConnectionError('offline')
    monkeypatch.setattr(mfa.requests,'request',unavailable)
    assert c.post('/api/auth/mfa/verify',json={'access_token':token(),'factor_id':FACTOR,'code':'123456'}).status_code==503


def test_shared_demo_cannot_enroll_a_personal_factor(client):
    from app.login_aliases import DEMO_AUTH_EMAIL
    c,user,calls=client;user['email']=DEMO_AUTH_EMAIL
    assert c.post('/api/auth/mfa/enroll',json={'access_token':token()}).status_code==403
    assert not calls
