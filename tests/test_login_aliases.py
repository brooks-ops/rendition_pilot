import pytest
from fastapi import HTTPException
from app.login_aliases import resolve_login_identifier,DEMO_AUTH_EMAIL
from backend import main


def test_alias_and_email_normalization():
    assert resolve_login_identifier(' DeMo ')==DEMO_AUTH_EMAIL
    assert resolve_login_identifier('yc-demo')==DEMO_AUTH_EMAIL
    assert resolve_login_identifier(' Appraiser@CAD.org ')=='appraiser@cad.org'
    assert resolve_login_identifier('another-user')=='another-user'


def test_demo_still_requires_valid_password_and_membership(monkeypatch):
    calls=[]
    def signin(identifier,password):
        calls.append((identifier,password))
        return {'access_token':'session','user':{'email':DEMO_AUTH_EMAIL,'id':'demo-user'}}
    monkeypatch.setattr(main,'sign_in_with_supabase',signin)
    monkeypatch.setattr(main,'build_district_context',lambda *a:None)
    with pytest.raises(HTTPException) as exc:main.auth_login(main.LoginRequest(email='demo',password='provided-password'))
    assert exc.value.status_code==403
    assert calls==[(DEMO_AUTH_EMAIL,'provided-password')]


def test_demo_does_not_bypass_authentication(monkeypatch):
    def reject(*a):raise HTTPException(401,'Invalid login credentials')
    monkeypatch.setattr(main,'sign_in_with_supabase',reject)
    monkeypatch.setattr(main,'build_district_context',lambda *a:pytest.fail('Invalid login must not resolve access'))
    with pytest.raises(HTTPException) as exc:main.auth_login(main.LoginRequest(email='demo',password='wrong'))
    assert exc.value.status_code==401
