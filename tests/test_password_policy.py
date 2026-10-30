import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from app.password_policy import validate_password
from backend import main

@pytest.mark.parametrize('password',['abcdefgh1!','ABCDEFGH9#','MixedCase12!'])
def test_accepts_eight_letters_digit_and_symbol(password):
    assert validate_password(password)==password

@pytest.mark.parametrize('password',['abcdefg1!','abcdefgh!','abcdefgh1','abcdefgh1 ','12345678!','abcdefgh'])
def test_rejects_missing_requirement(password):
    with pytest.raises(HTTPException) as exc:validate_password(password)
    assert exc.value.status_code==400

@pytest.mark.parametrize('path',['/api/auth/signup','/api/arb/auth/signup','/api/auth/change-password'])
def test_invalid_new_password_is_rejected_before_credentials_are_written(monkeypatch,path):
    monkeypatch.setattr(main,'create_supabase_account',lambda *a:pytest.fail('Weak password must not be submitted'))
    monkeypatch.setattr(main.requests,'put',lambda *a,**kw:pytest.fail('Weak password must not be submitted'))
    r=TestClient(main.app).post(path,json={'email':'user@example.invalid','password':'abcdefgh1','confirm_password':'abcdefgh1','access_token':'test'})
    assert r.status_code==400
    assert '8 letters' in r.json()['detail']
