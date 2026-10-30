import base64
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from app import security
from backend import main
from tests.test_backend_intelligence_queue import make_district


def token(sub='u', aal='aal1', email='admin@example.invalid'):
    return 'header.'+base64.urlsafe_b64encode(json.dumps({'sub':sub,'aal':aal,'email':email}).encode()).decode().rstrip('=')+'.signature'


def test_https_security_headers_and_no_inline_script_permission():
    c=TestClient(main.app,base_url='https://app.test')
    r=c.get('/')
    assert r.status_code==200
    assert 'max-age=' in r.headers['strict-transport-security']
    assert r.headers['x-content-type-options']=='nosniff'
    assert r.headers['x-frame-options']=='DENY'
    script=r.headers['content-security-policy'].split('script-src ')[1].split(';')[0]
    assert 'sha256-' in script and 'unsafe-inline' not in script and 'unsafe-eval' not in script
    assert "frame-ancestors 'none'" in r.headers['content-security-policy']


def test_session_cookie_secure_httponly_and_refresh_credentials_never_returned(monkeypatch):
    monkeypatch.setattr(main,'sign_in_with_supabase',lambda *a:dict(access_token='access',refresh_token='secret-renew',expires_in=3600,user={'id':'u'}))
    monkeypatch.setattr(main,'build_district_context',lambda *a:make_district(role='member'))
    monkeypatch.setattr(main,'_district_to_dict',lambda d:{'district_id':d.district_id,'role':d.role})
    c=TestClient(main.app,base_url='https://app.test')
    r=c.post('/api/auth/login',json={'email':'user@example.invalid','password':'test'})
    assert r.status_code==200 and 'refresh_token' not in r.json()
    assert r.json()['cookie_session'] is True
    cookie=r.headers['set-cookie']
    assert '__Secure-rp-refresh=secret-renew' in cookie and 'HttpOnly' in cookie and 'Secure' in cookie and 'SameSite=strict' in cookie and 'Path=/api/auth' in cookie
    def refresh(path,payload):
        assert payload=={'refresh_token':'secret-renew'}
        return dict(access_token='new',refresh_token='rotated',user={'id':'u'})
    monkeypatch.setattr(main,'supabase_auth_request',refresh)
    r=c.post('/api/auth/refresh',json={})
    assert r.status_code==200 and 'refresh_token' not in r.json() and c.cookies['__Secure-rp-refresh']=='rotated'
    monkeypatch.setattr(main,'supabase_auth_request',lambda *a:{'access_token':'logout-token'})
    monkeypatch.setattr(main.requests,'post',lambda *a,**kw:SimpleNamespace(status_code=204))
    assert c.post('/api/auth/logout').json()['provider_session_revoked'] is True
    assert '__Secure-rp-refresh' not in c.cookies


@pytest.mark.parametrize('headers',[{'Origin':'https://attacker.invalid'},{'Sec-Fetch-Site':'cross-site'}])
def test_cross_site_refresh_and_signout_rejected(headers):
    c=TestClient(main.app)
    for path in ('refresh','logout','login'):
        assert c.post('/api/auth/'+path,headers=headers,json={}).status_code==403


def test_password_only_session_allows_admin_and_preserves_membership_checks(monkeypatch):
    user={'id':'u','email':'admin@example.invalid','app_metadata':{}}
    monkeypatch.setattr(main,'get_supabase_user',lambda _:user)
    monkeypatch.setattr(main,'build_district_context',lambda *a:make_district(role='admin'))
    assert main.get_authenticated_district_context(token()).role=='admin'
    monkeypatch.setattr(main,'build_district_context',lambda *a:None)
    with pytest.raises(HTTPException):main.get_authenticated_district_context(token())


def test_owner_password_session_still_requires_server_owned_super_admin_claim():
    from app.super_admin import require_super_admin
    user={'id':'u','app_metadata':{'rendition_super_admin':True}}
    assert require_super_admin(lambda _:user,token())==user
    with pytest.raises(HTTPException):require_super_admin(lambda _:{'id':'u','app_metadata':{},'user_metadata':{'rendition_super_admin':True}},token())


def test_authenticator_routes_are_disabled():
    c=TestClient(main.app)
    for path in ('status','enroll','verify'):
        assert c.post('/api/auth/mfa/'+path,json={}).status_code==404


def test_legacy_profile_reads_and_schedule_writes_are_tenant_scoped(monkeypatch):
    district=make_district(role='member')
    monkeypatch.setattr(main,'get_authenticated_district_context',lambda _:district)
    monkeypatch.setattr(main,'require_access',lambda d,_:d)
    monkeypatch.setattr(main,'fetch_cad_profile_by_district_id',lambda _:pytest.fail('Should not read another district'))
    c=TestClient(main.app)
    for path in ('/api/cad-profile/other','/api/cad-schedules/other'):
        assert c.get(path,headers={'Authorization':'Bearer test'}).status_code==403
    assert c.delete('/api/cad-schedules/schedule',headers={'Authorization':'Bearer test'}).status_code==403
    monkeypatch.setattr(main,'get_authenticated_district_context',lambda _:replace(district,role='admin'))
    monkeypatch.setattr(main,'fetch_cad_profile_by_district_id',lambda _:{'id':'own-profile'})
    monkeypatch.setattr(main,'fetch_cad_schedule',lambda _:{'id':'schedule','district_id':'other-profile'})
    assert c.delete('/api/cad-schedules/schedule',headers={'Authorization':'Bearer test'}).status_code==403


def test_onboarding_cannot_assign_admin_membership_without_owner_auth(monkeypatch):
    from app import super_admin
    monkeypatch.setattr(main,'get_supabase_user',lambda _:{'id':'outsider','app_metadata':{}})
    monkeypatch.setattr(main,'create_or_update_district',lambda **kw:pytest.fail('Must not write before owner check'))
    c=TestClient(main.app)
    r=c.post('/api/auth/district-setup',json={'email':'x@example.invalid','password':'Password123!','confirm_password':'Password123!','district_name':'Existing county','admin_email':'x@example.invalid'})
    assert r.status_code==403
    payload={'district':{'cad_name':'Test','display_name':'Test','email':'x@example.invalid','phone':'555','address':'123'},'admin':{'first_name':'Test','last_name':'User','email':'x@example.invalid','role_title':'Chief'},'schedules':[]}
    assert c.post('/api/cad-onboarding',json=payload).status_code==403


def test_large_and_streamed_bodies_are_rejected_before_route_execution(monkeypatch):
    app=FastAPI();called=[]
    @app.post('/api/upload')
    def upload():called.append(True);return {}
    app.add_middleware(security.SecurityMiddleware,frontend=main.FRONTEND_DIR)
    monkeypatch.setattr(security,'MAX_BODY',100)
    c=TestClient(app)
    assert c.post('/api/upload',content='x'*101).status_code==413
    assert c.post('/api/upload',content=iter([b'x'*60,b'x'*60])).status_code==413
    assert not called


def test_rate_limits_expire_and_do_not_trust_user_forwarded_headers():
    limiter=security.SlidingLimits()
    assert limiter.allow('client',2,now=0)
    assert limiter.allow('client',2,now=1)
    assert not limiter.allow('client',2,now=2)
    assert limiter.allow('client',2,now=61)
    app=FastAPI()
    @app.post('/api/auth/login')
    def login():return {}
    app.add_middleware(security.SecurityMiddleware,frontend=main.FRONTEND_DIR)
    c=TestClient(app)
    for i in range(30):assert c.post('/api/auth/login',headers={'X-Forwarded-For':str(i)}).status_code==200
    r=c.post('/api/auth/login',headers={'X-Forwarded-For':'new-identity'})
    assert r.status_code==429 and r.headers['retry-after']=='60'


def test_shared_demo_cannot_change_password_accounts_or_queue_records():
    from app.login_aliases import DEMO_AUTH_EMAIL
    c=TestClient(main.app)
    demo=token(email=DEMO_AUTH_EMAIL)
    for path in ('/api/auth/change-password','/api/district/users/invite','/api/admin/intelligence/resolve','/api/commercial/apply','/api/arb/review'):
        r=c.post(path,json={'access_token':demo})
        assert r.status_code==403 and 'read-only' in r.json()['detail']


def test_demo_resolution_fails_closed_if_membership_is_moved_to_a_live_county(monkeypatch):
    from app.login_aliases import DEMO_AUTH_EMAIL
    monkeypatch.setattr(main,'resolve_district_for_user',lambda **kw:make_district(role='admin'))
    with pytest.raises(HTTPException) as exc:main.build_district_context({'email':DEMO_AUTH_EMAIL,'id':'u'},'token')
    assert exc.value.status_code==403


def test_render_tls_termination_still_sets_secure_cookie_and_accepts_same_origin(monkeypatch):
    monkeypatch.setenv('RENDER_EXTERNAL_URL','https://service.onrender.com')
    app=FastAPI()
    @app.post('/api/auth/test')
    def auth_test():return {}
    app.add_middleware(security.SecurityMiddleware,frontend=main.FRONTEND_DIR)
    c=TestClient(app,base_url='http://www.renditionpilot.com')
    response=c.post('/api/auth/test',headers={'Origin':'https://www.renditionpilot.com'})
    assert response.status_code==200 and response.headers['strict-transport-security']
    from starlette.requests import Request
    request=Request({'type':'http','scheme':'http','headers':[]})
    assert security.secure_request(request)
