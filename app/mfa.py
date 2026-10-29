"""Supabase TOTP enrollment/challenge using each user's verified session."""
import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from app import security
from app.login_aliases import DEMO_AUTH_EMAIL

class Session(BaseModel):
    access_token: str = Field(min_length=1, max_length=10000)

class Verify(Session):
    factor_id: str = Field(pattern=r'^[a-fA-F0-9-]{36}$')
    code: str = Field(pattern=r'^\d{6}$')


def router(get_user, build_district, config, district_dict):
    api = APIRouter(prefix='/api/auth/mfa')

    def context(token):
        user = get_user(token)
        district = build_district(user, token)
        if not district or district.must_change_password:
            raise HTTPException(403, 'Set your password and link your district before setting up an authenticator.')
        if user.get('email', '').lower() == DEMO_AUTH_EMAIL:
            raise HTTPException(403, 'Authenticator changes are disabled for the shared demonstration account.')
        return user, district

    def call(token, path, data=None, method='POST'):
        url, key = config()
        try:
            response = requests.request(method, url+'/auth/v1/'+path,
                headers={'apikey':key, 'Authorization':'Bearer '+token}, json=data, timeout=20)
        except (requests.ConnectionError, requests.Timeout):
            raise HTTPException(503, 'Authenticator service is temporarily unavailable. Please retry.') from None
        if response.status_code >= 400:
            raise HTTPException(400, 'Authenticator verification failed. Check the code and your phone clock, then retry.')
        return response.json() if response.content else {}

    @api.post('/status')
    def status(request: Session):
        user, district = context(request.access_token)
        factors = [{'id':f['id'], 'friendly_name':f.get('friendly_name')}
                   for f in (user.get('factors') or []) if f.get('factor_type')=='totp' and f.get('status')=='verified']
        return {'required':security.mfa_required(user, request.access_token, district.role), 'factors':factors}

    @api.post('/enroll')
    def enroll(request: Session):
        user, district = context(request.access_token)
        factors = (user.get('factors') or [])
        if any(f.get('status')=='verified' and f.get('factor_type')=='totp' for f in factors):
            raise HTTPException(409, 'An authenticator is already set up. Use its current code to continue.')
        # Remove abandoned, unverified enrollments only. Verified factors are never deleted here.
        for f in factors:
            if f.get('factor_type')=='totp' and f.get('status')=='unverified':
                call(request.access_token, 'factors/'+f['id'], method='DELETE')
        result = call(request.access_token, 'factors', {'factor_type':'totp','friendly_name':'Rendition Pilot','issuer':'Rendition Pilot'})
        return {'factor_id':result['id'], 'secret':result['totp']['secret'], 'qr_code':result['totp']['qr_code']}

    @api.post('/verify')
    def verify(request: Verify):
        user, district = context(request.access_token)
        if not any(f.get('id')==request.factor_id and f.get('factor_type')=='totp' for f in (user.get('factors') or [])):
            raise HTTPException(403, 'Authenticator does not belong to this account.')
        challenge = call(request.access_token, 'factors/'+request.factor_id+'/challenge', {})
        result = call(request.access_token, 'factors/'+request.factor_id+'/verify', {'challenge_id':challenge['id'],'code':request.code})
        token = result['access_token']
        verified = get_user(token)
        if verified.get('id')!=user.get('id'):
            raise HTTPException(403, 'The verified session does not belong to this account.')
        security.enforce_mfa(verified, token, 'admin')
        linked = build_district(verified, token)
        if not linked or linked.district_id!=district.district_id:
            raise HTTPException(403, 'District membership changed. Sign in again.')
        return {**{k:result[k] for k in ('access_token','refresh_token','expires_in','expires_at','token_type') if k in result},
                'user':verified, 'district':district_dict(linked), 'mfa_required':False}
    return api
