"""Server-managed workspace grants, including invitations without an auth user yet."""
import hashlib
import json
import requests
from fastapi import HTTPException
from app.commercial.remote import object_request


def permission_name(email):
    return 'workspace-access-'+hashlib.sha256(email.strip().lower().encode()).hexdigest()+'.json'


def _read_grants(district_id,name):
    try:
        response=object_request('GET',district_id,name)
        if response.status_code==404 or (response.status_code==400 and response.json().get('statusCode')=='404'):
            return {'bpp':True,'commercial':True}
        response.raise_for_status();data=response.json()
        if any(type(data.get(key)) is not bool for key in ('bpp','commercial')):raise ValueError('Invalid grants')
        return {key:data[key] for key in ('bpp','commercial')}
    except (requests.RequestException,ValueError) as exc:raise HTTPException(503,'Workspace permissions could not be verified. Please try again.') from exc


def district_access(district_id):
    return _read_grants(district_id,'workspace-policy.json')


def read_access(district_id,email):
    grants=_read_grants(district_id,permission_name(email));limits=district_access(district_id)
    return {key:grants[key] and limits[key] for key in ('bpp','commercial')}


def access_for(district):
    if district.role=='admin':return district_access(district.district_id)
    return read_access(district.district_id,district.email)


def require_access(district,workspace):
    if not access_for(district)[workspace]:raise HTTPException(403,f'Your account does not have {"Commercial" if workspace=="commercial" else "BPP"} access. Contact your district administrator.')
    return district


def save_access(district_id,email,bpp,commercial,actor):
    if not (bpp or commercial):raise HTTPException(400,'Select BPP, Commercial, or both.')
    from datetime import datetime,timezone
    data={'bpp':bool(bpp),'commercial':bool(commercial),'updated_by':actor,'updated_at':datetime.now(timezone.utc).isoformat()}
    try:object_request('POST',district_id,permission_name(email),json.dumps(data).encode()).raise_for_status()
    except requests.RequestException as exc:raise HTTPException(503,'Workspace access could not be saved. Please try again.') from exc
    return {key:data[key] for key in ('bpp','commercial')}
