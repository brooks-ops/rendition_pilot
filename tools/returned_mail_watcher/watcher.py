"""Windows, standard-library-only returned-mail uploader. Never changes source files."""
import argparse
import base64
import ctypes
import getpass
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import urllib.error
import urllib.request

ROOT = r'T:\Public\Appraisal\BP\2027\Returned Mail'
ORIGIN = 'https://www.renditionpilot.com'
MAX_BYTES = 12 * 1024 * 1024


def protect(data, decrypt=False):
    """DPAPI binds session storage to this Windows login; no password saved."""
    if os.name != 'nt':
        raise RuntimeError('Session storage requires Windows DPAPI.')
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_byte))]
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    output = Blob()
    if decrypt:
        ok = ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output))
    else:
        ok = ctypes.windll.crypt32.CryptProtectData(ctypes.byref(source), 'Rendition Pilot', None, None, None, 1, ctypes.byref(output))
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        ctypes.windll.kernel32.LocalFree(output.data)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # Never forward credentials or scans to another endpoint.


class Client:
    def __init__(self, state):
        self.path = state / 'session.dpapi'
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPCookieProcessor(self.jar))
        self.token = None

    def post(self, path, body):
        request = urllib.request.Request(ORIGIN + path, json.dumps(body).encode(),
            {'Content-Type': 'application/json', 'Origin': ORIGIN}, method='POST')
        with self.opener.open(request, timeout=180) as response:
            return json.load(response)

    def save(self):
        values = [dict(vars(cookie)) for cookie in self.jar]
        temporary = self.path.with_suffix('.tmp')
        temporary.write_bytes(protect(json.dumps(values).encode()))
        os.replace(temporary, self.path)

    def login(self, identifier, password):
        self.post('/api/auth/login', {'email': identifier, 'password': password})
        if not list(self.jar):
            raise RuntimeError('Server did not establish a renewable session.')
        self.save()

    def refresh(self):
        if not list(self.jar):
            for value in json.loads(protect(self.path.read_bytes(), decrypt=True)):
                value['rest'] = value.pop('_rest', {})
                self.jar.set_cookie(http.cookiejar.Cookie(**value))
        result = self.post('/api/auth/refresh', {})
        self.token = result['access_token']
        self.save()  # Persist rotated cookie before doing work.

    def upload(self, relative, data):
        body = {'access_token': self.token, 'relative_path': relative,
                'file_base64': base64.b64encode(data).decode()}
        try:
            return self.post('/api/admin/intelligence/returned-mail/import', body)
        except urllib.error.HTTPError as exc:
            if exc.code != 401:
                raise
            self.refresh()
            body['access_token'] = self.token
            return self.post('/api/admin/intelligence/returned-mail/import', body)


def database(state):
    db = sqlite3.connect(state / 'progress.sqlite')
    db.execute('CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, signature TEXT, stable_since REAL, digest TEXT, status TEXT, attempts INTEGER DEFAULT 0, retry_at REAL DEFAULT 0, error TEXT)')
    db.commit()
    return db


def scan(root, db, upload, now=None):
    """One pass; stable files only. Failures retry with capped backoff."""
    now = time.time() if now is None else now
    if not root.is_dir():
        raise RuntimeError('Scan folder unavailable. Check the T: drive and office network.')
    for folder in sorted(root.iterdir()):
        if folder.is_symlink() or not folder.is_dir() or not re.fullmatch(r'[A-Za-z]?\d{1,20}', folder.name):
            continue
        for path in sorted(folder.iterdir()):
            if path.is_symlink() or not path.is_file() or path.suffix.lower() not in ('.pdf', '.jpg', '.jpeg', '.png'):
                continue
            relative = folder.name + '/' + path.name
            row = None
            try:
                stat = path.stat()
                signature = f'{stat.st_size}:{stat.st_mtime_ns}'
                row = db.execute('SELECT signature,stable_since,digest,status,attempts,retry_at FROM files WHERE path=?', (relative,)).fetchone()
                if not row or row[0] != signature:
                    db.execute('INSERT OR REPLACE INTO files(path,signature,stable_since,status) VALUES(?,?,?,?)', (relative, signature, now, 'WAITING'))
                    db.commit()
                    continue
                if row[3] == 'UPLOADED' or now - row[1] < 60 or now < (row[5] or 0):
                    continue
                if stat.st_size == 0 or stat.st_size > MAX_BYTES:
                    raise ValueError('File must be between 1 byte and 12 MiB.')
                with path.open('rb') as source:
                    data = source.read(MAX_BYTES + 1)
                after = path.stat()
                if f'{after.st_size}:{after.st_mtime_ns}' != signature or len(data) != stat.st_size:
                    continue
                digest = hashlib.sha256(data).hexdigest()
                upload(relative, data)  # Server dedup is authoritative, including retries after crashes.
                db.execute('UPDATE files SET digest=?,status=?,error=NULL,attempts=0,retry_at=0 WHERE path=?', (digest, 'UPLOADED', relative))
                db.commit()
            except (OSError, ValueError, urllib.error.HTTPError) as exc:
                # Never store response bodies, tokens, OCR, or recipient addresses in logs.
                error = ('HTTP ' + str(exc.code)) if isinstance(exc, urllib.error.HTTPError) else type(exc).__name__
                db.execute('UPDATE files SET status=?,attempts=COALESCE(attempts,0)+1,retry_at=?,error=? WHERE path=?',
                           ('RETRY', now + min(3600, 60 * 2 ** min((row[4] or 0) if row else 0, 6)), error, relative))
                db.commit()
                if isinstance(exc, urllib.error.HTTPError) and exc.code in (401, 403, 404, 429, 503):
                    return  # Avoid hammering a stopped, unavailable, or rate-limited server.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['setup', 'run', 'status'])
    args = parser.parse_args()
    state = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'RenditionPilot' / 'ReturnedMail'
    state.mkdir(parents=True, exist_ok=True)
    # Task Scheduler also uses IgnoreNew; this lock protects manual invocations.
    if os.name != 'nt':
        raise RuntimeError('Run this helper on the Windows office computer.')
    import msvcrt
    with (state / 'watcher.lock').open('a+b') as lock:
        lock.seek(0); lock.write(b'0'); lock.flush(); lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return
        client = Client(state)
        db = database(state)
        if args.action == 'setup':
            client.login(input('Rendition Pilot username: ').strip(), getpass.getpass('Password: '))
            print('Session saved securely for this Windows user. No password saved.')
        elif args.action == 'status':
            for status, count in db.execute('SELECT status,count(*) FROM files GROUP BY status'):
                print(status, count)
            for path, error in db.execute("SELECT path,error FROM files WHERE status='RETRY' LIMIT 20"):
                print(path, error)
            if (state / 'last-run.json').exists():
                print((state / 'last-run.json').read_text())
        else:
            try:
                client.refresh()
                scan(Path(ROOT), db, client.upload)
                retries = db.execute("SELECT count(*) FROM files WHERE status='RETRY'").fetchone()[0]
                result = {'at': time.time(), 'status': 'RETRY_PENDING' if retries else 'PASS_COMPLETE', 'retry_files': retries}
            except Exception as exc:
                result = {'at': time.time(), 'status': 'FAILED', 'error': type(exc).__name__}
                if isinstance(exc, urllib.error.HTTPError):
                    result['http_status'] = exc.code
                raise
            finally:
                (state / 'last-run.json').write_text(json.dumps(result))


if __name__ == '__main__':
    main()
