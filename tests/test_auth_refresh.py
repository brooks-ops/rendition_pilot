import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from backend import main
from tests.test_backend_intelligence_queue import make_district

@pytest.fixture
def auth(monkeypatch):
    monkeypatch.setattr(main,'build_district_context',lambda *a:make_district(role='member'))
    monkeypatch.setattr(main,'_district_to_dict',lambda d:{'district_id':d.district_id,'role':d.role})
    return TestClient(main.app)

def test_login_keeps_refresh_credentials_and_expiry(auth,monkeypatch):
    monkeypatch.setattr(main,'sign_in_with_supabase',lambda *a:dict(access_token='access',refresh_token='refresh',expires_in=3600,expires_at=123,user={'id':'u'}))
    response=auth.post('/api/auth/login',json={'email':'member@example.invalid','password':'test-only'})
    assert response.status_code==200 and 'refresh_token' not in response.json() and response.cookies['rp-refresh-local']=='refresh'
    assert response.json()['expires_at']==123 and response.headers['cache-control']=='no-store'

def test_refresh_rotates_tokens_and_rechecks_district(auth,monkeypatch):
    def renew(path,payload):
        assert path=='token?grant_type=refresh_token' and payload=={'refresh_token':'old-refresh'}
        return dict(access_token='new-access',refresh_token='new-refresh',expires_in=3600,user={'id':'u'})
    monkeypatch.setattr(main,'supabase_auth_request',renew)
    response=auth.post('/api/auth/refresh',json={'refresh_token':'old-refresh'})
    assert response.status_code==200 and 'refresh_token' not in response.json() and response.cookies['rp-refresh-local']=='new-refresh'
    assert response.json()['district']['role']=='member' and response.headers['cache-control']=='no-store'

def test_invalid_refresh_is_auth_error_and_does_not_resolve_access(auth,monkeypatch):
    def deny(*a):raise HTTPException(400,'Refresh token not found')
    monkeypatch.setattr(main,'supabase_auth_request',deny)
    monkeypatch.setattr(main,'build_district_context',lambda *a:pytest.fail('Invalid credential'))
    response=auth.post('/api/auth/refresh',json={'refresh_token':'invalid'})
    assert response.status_code==401 and 'sign in again' in response.json()['detail']

def test_refresh_does_not_restore_removed_district_access(auth,monkeypatch):
    monkeypatch.setattr(main,'supabase_auth_request',lambda *a:dict(access_token='new',refresh_token='new-r',user={'id':'u'}))
    monkeypatch.setattr(main,'build_district_context',lambda *a:None)
    assert auth.post('/api/auth/refresh',json={'refresh_token':'old'}).status_code==403

def test_renewal_outage_is_retryable(auth,monkeypatch):
    import requests
    def offline(*a):raise requests.ConnectionError('offline')
    monkeypatch.setattr(main,'supabase_auth_request',offline)
    assert auth.post('/api/auth/refresh',json={'refresh_token':'old'}).status_code==503


def test_provider_jwt_expiry_is_normalized_for_automatic_retry(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(main,'get_supabase_config',lambda:('https://example.invalid','anon'))
    monkeypatch.setattr(main.requests,'get',lambda *a,**kw:SimpleNamespace(status_code=403,json=lambda:{'message':'JWT expired'}))
    with pytest.raises(HTTPException) as exc:main.get_supabase_user('expired')
    assert exc.value.status_code==401 and 'JWT' not in exc.value.detail
