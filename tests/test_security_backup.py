import io
import json
import zipfile
from types import SimpleNamespace
import pytest
from cryptography.exceptions import InvalidTag
from app import security_backup as b


def test_backup_pagination_does_not_truncate_short_api_pages(monkeypatch):
    offsets=[]
    def request(method,path,**kw):
        if path=='/rest/v1/':data={'definitions':{'sample':{'properties':{'id':{'description':'<pk/>'}}}}}
        elif path=='/storage/v1/bucket':data=[]
        else:
            offset=kw['params']['offset'];offsets.append(offset);data=[{'id':offset}]
        return SimpleNamespace(json=lambda:data,headers={'Content-Range':'0-0/3'})
    monkeypatch.setattr(b,'request',request)
    archive,manifest=b.create_archive()
    assert manifest['tables']['sample']['rows']==3 and offsets==[0,1,2]
    assert b.verify(archive)==manifest


def test_backup_roundtrip_and_tampering_detection():
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
    private=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    public=private.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo)
    secret=private.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption())
    encrypted=b.encrypt(b'sensitive taxpayer data',public)
    assert b'sensitive taxpayer data' not in encrypted
    assert b.decrypt(encrypted,secret)==b'sensitive taxpayer data'
    with pytest.raises(InvalidTag):b.decrypt(encrypted[:-1]+bytes([encrypted[-1]^1]),secret)


def test_recovery_drill_refuses_an_existing_or_production_database():
    with pytest.raises(ValueError):b.restore_drill(b'','postgres')


def test_transient_storage_520_is_retried_only_for_idempotent_operations(monkeypatch):
    import requests
    calls=[]
    def fake(*a,**kw):
        r=requests.Response();r.status_code=520 if not calls else 200;r._content=b'{}';calls.append(True);return r
    monkeypatch.setattr(b,'config',lambda:('https://storage.test',{}))
    monkeypatch.setattr(b.requests,'request',fake)
    monkeypatch.setattr(b.time,'sleep',lambda _:None)
    assert b.request('POST','/storage/v1/object/security-backups/part',headers={'x-upsert':'true'}).status_code==200
    assert len(calls)==2
    calls.clear()
    with pytest.raises(requests.HTTPError):b.request('POST','/other-write',json={})
    assert len(calls)==1
