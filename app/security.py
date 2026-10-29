"""HTTP hardening for the single Render web process (no credentials in logs)."""
import base64
import hashlib
import json
import os
import re
import time
from collections import OrderedDict, deque
from pathlib import Path
from threading import Lock

from fastapi import HTTPException
from starlette.responses import JSONResponse

REFRESH_COOKIE = '__Secure-rp-refresh'
LOCAL_REFRESH_COOKIE = 'rp-refresh-local'
COOKIE_PATH = '/api/auth'
MAX_BODY = 48 * 1024 * 1024


def token_claims(token):
    # Authorization callers MUST verify this token with Supabase /user first.
    try:
        if not isinstance(token,str):return {}
        encoded = token.split('.')[1]
        claims=json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
        return claims if isinstance(claims,dict) else {}
    except (ValueError, IndexError, TypeError):
        return {}


def mfa_required(user, token, role=None):
    privileged = role == 'admin' or (user.get('app_metadata') or {}).get('rendition_super_admin') is True
    if not privileged:
        return False
    claims = token_claims(token)
    return claims.get('sub') != user.get('id') or claims.get('aal') != 'aal2'


def enforce_mfa(user, token, role=None):
    if mfa_required(user, token, role):
        raise HTTPException(403, 'Verify your authenticator in Rendition Pilot before using administrator tools.')


def secure_request(request):
    return request.url.scheme == 'https'


def refresh_cookie(request):
    return request.cookies.get(REFRESH_COOKIE if secure_request(request) else LOCAL_REFRESH_COOKIE)


def set_refresh_cookie(response, request, token):
    response.set_cookie(REFRESH_COOKIE if secure_request(request) else LOCAL_REFRESH_COOKIE,
                        token, httponly=True, secure=secure_request(request), samesite='strict',
                        path=COOKIE_PATH, max_age=60 * 60 * 24 * 30)


def clear_refresh_cookie(response, request):
    response.delete_cookie(REFRESH_COOKIE if secure_request(request) else LOCAL_REFRESH_COOKIE,
                           path=COOKIE_PATH, httponly=True, secure=secure_request(request), samesite='strict')


class SlidingLimits:
    def __init__(self):
        self.entries = OrderedDict()
        self.lock = Lock()

    def allow(self, key, maximum, window=60, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            hits = self.entries.setdefault(key, deque())
            while hits and hits[0] <= now - window:
                hits.popleft()
            self.entries.move_to_end(key)
            if len(hits) >= maximum:
                return False
            hits.append(now)
            # Bound memory even under random IP/token traffic.
            while len(self.entries) > 10000:
                self.entries.popitem(last=False)
            return True


class SecurityMiddleware:
    """Body limits apply to streamed requests as well as Content-Length."""
    def __init__(self, app, frontend):
        self.app = app
        self.limits = SlidingLimits()
        self.heavy_active = 0
        hashes = set()
        for path in Path(frontend).glob('*.html'):
            for attrs, source in re.findall(r'<script\b([^>]*)>(.*?)</script\s*>', path.read_text(), re.S | re.I):
                if not re.search(r'\bsrc\s*=', attrs, re.I):
                    hashes.add("'sha256-" + base64.b64encode(hashlib.sha256(source.encode()).digest()).decode() + "'")
        self.csp = "; ".join([
            "default-src 'self'", "script-src 'self' " + ' '.join(sorted(hashes)),
            "style-src 'self' 'unsafe-inline'", "img-src 'self' data: blob:",
            "connect-src 'self'", "font-src 'self'", "frame-src 'self' blob:",
            "object-src 'none'", "base-uri 'none'", "form-action 'self'", "frame-ancestors 'none'",
        ])

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = dict(scope.get('headers', []))
        path = scope['path']
        async def reject(code, detail, retry=False):
            response = JSONResponse({'detail': detail}, status_code=code,
                                    headers={'Retry-After': '60'} if retry else {})
            await response(scope, receive, hardened_send)

        async def hardened_send(message):
            if message['type'] == 'http.response.start':
                response_headers = dict(message.get('headers', []))
                response_headers.update({
                    b'content-security-policy': self.csp.encode(),
                    b'x-content-type-options': b'nosniff', b'x-frame-options': b'DENY',
                    b'referrer-policy': b'strict-origin-when-cross-origin',
                    b'permissions-policy': b'camera=(self), microphone=(), geolocation=(self)',
                })
                if scope.get('scheme') == 'https':
                    response_headers[b'strict-transport-security'] = b'max-age=31536000'
                if path.startswith('/api/'):
                    response_headers[b'cache-control'] = b'no-store'
                # Keep duplicate Set-Cookie headers, if any.
                cookies = [(k,v) for k,v in message.get('headers', []) if k.lower() == b'set-cookie']
                response_headers.pop(b'set-cookie', None)
                message = {**message, 'headers': list(response_headers.items()) + cookies}
            await send(message)

        if path.startswith('/api/'):
            # Cookie-bearing session endpoints must only be called by this origin.
            origin = headers.get(b'origin', b'').decode()
            host = headers.get(b'host', b'').decode()
            same_origin = origin == scope.get('scheme', 'http') + '://' + host
            if (path.startswith('/api/auth/') and scope['method'] != 'OPTIONS' and
                (headers.get(b'sec-fetch-site') == b'cross-site' or (origin and not same_origin))):
                return await reject(403, 'Cross-site sign-in requests are not permitted.')
            try:
                length = int(headers.get(b'content-length', b'0'))
            except ValueError:
                return await reject(400, 'Invalid request size.')
            if length > MAX_BODY:
                return await reject(413, 'Upload exceeds the 48 MB request limit. Split the upload and try again.')
            if scope['method'] != 'OPTIONS':
                # Uvicorn's proxy middleware resolves the client using its trusted proxy configuration.
                # Never trust arbitrary X-Forwarded-For in application code.
                client = (scope.get('client') or ('unknown',))[0]
                if not self.limits.allow(('ip',client),600):
                    return await reject(429, 'Too many requests. Wait one minute and try again.', True)
                if path in ('/api/auth/login', '/api/auth/signup', '/api/auth/district-setup', '/api/arb/auth/login', '/api/arb/auth/signup'):
                    if not self.limits.allow(('login',client), 30):
                        return await reject(429, 'Too many sign-in attempts. Wait one minute and try again.', True)
                else:
                    credential = headers.get(b'authorization') or str(client).encode()
                    digest = hashlib.sha256(credential).hexdigest()
                    maximum = 15 if path.startswith('/api/auth/mfa/') else 180
                    category = 'mfa' if path.startswith('/api/auth/mfa/') else 'api'
                    if not self.limits.allow((category,digest), maximum):
                        return await reject(429, 'Too many requests. Wait one minute and try again.', True)
            # Collect API bodies with a bound before downstream JSON parsing allocates memory.
            if scope['method'] in ('POST','PUT','PATCH'):
                chunks = []
                total = 0
                while True:
                    event = await receive()
                    if event['type'] == 'http.disconnect':
                        return
                    total += len(event.get('body', b''))
                    if total > MAX_BODY:
                        return await reject(413, 'Upload exceeds the 48 MB request limit.')
                    chunks.append(event.get('body', b''))
                    if not event.get('more_body', False):
                        break
                body = b''.join(chunks)
                from app.login_aliases import DEMO_AUTH_EMAIL
                try:
                    payload = json.loads(body) if body else {}
                except (ValueError, UnicodeDecodeError):
                    payload = {}
                token = headers.get(b'authorization', b'').decode().removeprefix('Bearer ').strip()
                token = token or (payload.get('access_token','') if isinstance(payload,dict) else '')
                if str(token_claims(token).get('email') or '').lower() == DEMO_AUTH_EMAIL:
                    demo_reads = {
                        '/api/auth/restore','/api/auth/logout','/api/auth/refresh',
                        '/api/district/users','/api/admin/intelligence/summary','/api/admin/intelligence/queue',
                        '/api/admin/intelligence/item','/api/admin/intelligence/work-status',
                        '/api/admin/intelligence/cad-candidates','/api/admin/intelligence/cad-health',
                        '/api/admin/intelligence/account-card','/api/admin/intelligence/account-cards/completed',
                        '/api/admin/property/lookup','/api/admin/activity',
                        '/api/admin/comptroller/status','/api/admin/comptroller/review-queue',
                        '/api/admin/production-readiness','/api/county-clerk/collection-status','/api/county-clerk/sync-status','/api/county-clerk/reviewers',
                        '/api/commercial/session','/api/commercial/catalog','/api/commercial/category-summary',
                        '/api/commercial/property','/api/commercial/history',
                    }
                    if path not in demo_reads:
                        return await reject(403, 'This demonstration account is read-only. Live county accounts are separate.')

                sent = False
                async def bounded_receive():
                    nonlocal sent
                    if not sent:
                        sent = True
                        return {'type':'http.request','body':body,'more_body':False}
                    return await receive()
                heavy = path in ('/api/review/run','/api/pdf/render','/api/arb/review','/api/arb/evidence-packet','/api/commercial/sketch/generate')
                if heavy and self.heavy_active >= 2:
                    return await reject(429, 'Two document jobs are already running. Wait for one to finish, then retry.', True)
                if heavy:self.heavy_active += 1
                try:
                    return await self.app(scope, bounded_receive, hardened_send)
                finally:
                    if heavy:self.heavy_active -= 1
        await self.app(scope, receive, hardened_send)
