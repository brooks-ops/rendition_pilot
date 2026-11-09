"""Windows returned-mail uploader. Confirmed scans are filed in Uploaded."""
import argparse
import base64
import ctypes
import csv
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
import uuid

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
    columns = {row[1] for row in db.execute('PRAGMA table_info(files)')}
    for name in ('receipt', 'archived_path'):
        if name not in columns:
            db.execute('ALTER TABLE files ADD COLUMN ' + name + ' TEXT')
    db.commit()
    return db


def scan_files(root):
    """Read flat account-named scans and retain compatibility with account folders.

    upload_path is an API routing value; account-number folders are not created.
    Local progress uses the actual path so flat/nested files cannot collide.
    """
    extensions = ('.pdf', '.jpg', '.jpeg', '.png')
    for entry in sorted(root.iterdir()):
        if entry.is_symlink():
            continue
        if entry.is_file():
            if entry.suffix.lower() in extensions and re.fullmatch(r'[A-Za-z]?\d{1,20}', entry.stem):
                yield entry, entry.name, entry.stem.upper() + '/' + entry.name
        elif entry.is_dir() and re.fullmatch(r'[A-Za-z]?\d{1,20}', entry.name):
            for path in sorted(entry.iterdir()):
                if not path.is_symlink() and path.is_file() and path.suffix.lower() in extensions:
                    relative = entry.name + '/' + path.name
                    yield path, relative, relative


def upload_receipt(result, account):
    """A successful HTTP response must identify saved evidence for this account."""
    if not isinstance(result, dict) or result.get('account') != account:
        raise ValueError('Upload did not confirm the account; original PDF kept.')
    try:
        uuid.UUID(str(result['document_id']))
        uuid.UUID(str(result['item_id']))
    except (KeyError, ValueError, TypeError, AttributeError):
        raise ValueError('Upload did not confirm saved evidence; original PDF kept.') from None
    return {k: result[k] for k in ('document_id', 'item_id', 'account')}


def archive_uploaded(root, path, data, signature, account, previous=None, record_copy=None):
    """Copy confirmed bytes without overwrite, verify, then remove the inbox copy."""
    def unchanged():
        current = path.stat()
        if f'{current.st_size}:{current.st_mtime_ns}' != signature:
            return False
        with path.open('rb') as source:
            return source.read(MAX_BYTES + 1) == data
    if not unchanged():
        raise ValueError('File changed during upload; waiting for the completed scan. Original kept.')
    folder = root / 'Uploaded'
    if folder.is_symlink():
        raise OSError('Uploaded folder must not be a symbolic link.')
    folder.mkdir(exist_ok=True)
    filename = path.name if path.stem.upper() == account else account + '-' + path.name
    # Recover a verified copy after a crash or a denied inbox deletion.
    if previous:
        candidate = root / previous
        if candidate.parent == folder and not candidate.is_symlink() and candidate.is_file():
            with candidate.open('rb') as saved:
                if saved.read(MAX_BYTES + 1) == data:
                    if record_copy:
                        record_copy(previous)
                    if not unchanged():
                        raise ValueError('File changed during filing; current original kept for retry.')
                    path.unlink()
                    return previous
    target = folder / filename
    while True:
        try:
            output = target.open('xb')  # Exclusive creation: never overwrite another PDF.
            break
        except FileExistsError:
            target = folder / (Path(filename).stem[:160] + '__' + uuid.uuid4().hex[:12] + path.suffix)
    complete = False
    try:
        with output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        with target.open('rb') as saved:
            if saved.read(MAX_BYTES + 1) != data:
                raise OSError('The Uploaded copy could not be verified.')
        complete = True
        relative = target.relative_to(root).as_posix()
        if record_copy:
            record_copy(relative)  # Durable before removing the inbox copy.
        if not unchanged():
            raise ValueError('File changed during filing; saved version retained in Uploaded and current original kept for retry.')
        path.unlink()  # Only after server acknowledgement AND verified local copy.
        return relative
    finally:
        if not complete:
            # Only discard the partial copy created exclusively by this operation.
            try:
                target.unlink()
            except OSError:
                pass  # Original is still present; report a filing failure.


def failure_message(exc):
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 400:
            try:
                detail = json.loads(exc.read(8192)).get('detail')
                if isinstance(detail, str):
                    return 'HTTP 400: ' + ' '.join(detail.split())[:500]
            except (ValueError, OSError):
                pass
        return {401: 'Sign in again using watcher.py setup.', 403: 'Sign-in or BPP access was rejected; check the scanner login.',
                429: 'Server busy; automatic retry scheduled.', 502: 'Server could not save the scan; automatic retry scheduled.',
                503: 'Service unavailable; automatic retry scheduled.'}.get(exc.code, 'HTTP ' + str(exc.code))
    if isinstance(exc, ValueError):
        return str(exc)
    return 'Network or file access error; original kept for automatic retry.'


def write_upload_report(root, db):
    """Office-readable result; no session credentials, OCR or recipient addresses."""
    target = root / '_Upload status.csv'
    temporary = root / ('.upload-status-' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8-sig', newline='') as output:
            writer = csv.writer(output)
            def write_row(values):
                writer.writerow(["'"+str(v) if str(v).startswith(('=', '+', '-', '@')) else v for v in values])
            write_row(['File', 'Upload status', 'Details', 'Uploaded copy'])
            for relative, status, error, archived in db.execute('SELECT path,status,error,archived_path FROM files ORDER BY path'):
                if not (root / relative).is_file() and not (archived and (root / archived).is_file()):
                    continue
                label = {'ARCHIVED': 'Uploaded', 'UPLOADED': 'Uploaded; filing pending',
                         'ARCHIVE_RETRY': 'Uploaded; filing needs attention', 'WAITING': 'Waiting for scan to finish',
                         'RETRY': 'Needs attention / retry scheduled'}.get(status, status)
                details = error or ('Saved in Rendition Pilot. Address still requires appraiser review.' if status in ('ARCHIVED','UPLOADED') else '')
                write_row([relative, label, details, archived or ''])
            for path in sorted(root.iterdir()):
                if not path.is_symlink() and path.is_file() and path.suffix.lower() == '.pdf' and not re.fullmatch(r'[A-Za-z]?\d{1,20}', path.stem):
                    write_row([path.name, 'Needs attention', 'Filename must be the exact account number, for example P105330.pdf.', ''])
        os.replace(temporary, target)
        return None
    except OSError:
        return 'Upload status report could not be saved. Close it in Excel and check shared-folder write access.'
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def scan(root, db, upload, now=None, archive=False):
    """One pass; stable files only. Production enables filing after confirmation."""
    now = time.time() if now is None else now
    if not root.is_dir():
        raise RuntimeError('Scan folder unavailable. Check the T: drive and office network.')
    entries = list(scan_files(root))
    summary = {'eligible_files': len(entries)}
    if archive:
        for relative, in db.execute("SELECT path FROM files WHERE status IN ('RETRY','ARCHIVE_RETRY','WAITING')").fetchall():
            if not (root / relative).is_file():
                db.execute("UPDATE files SET status='NOT_IN_INBOX',error=NULL WHERE path=?", (relative,))
        db.commit()
    for path, relative, upload_path in entries:
        row = None
        confirmed = False
        try:
            stat = path.stat()
            signature = f'{stat.st_size}:{stat.st_mtime_ns}'
            row = db.execute('SELECT signature,stable_since,digest,status,attempts,retry_at,receipt,archived_path FROM files WHERE path=?', (relative,)).fetchone()
            if not row or row[0] != signature or row[3] == 'ARCHIVED':
                db.execute('INSERT OR REPLACE INTO files(path,signature,stable_since,status) VALUES(?,?,?,?)', (relative, signature, now, 'WAITING'))
                db.commit()
                continue
            if (row[3] == 'UPLOADED' and not archive) or now - row[1] < 60 or now < (row[5] or 0):
                continue
            if stat.st_size == 0 or stat.st_size > MAX_BYTES:
                raise ValueError('File is empty or exceeds the 12 MiB upload limit. Original kept.')
            with path.open('rb') as source:
                data = source.read(MAX_BYTES + 1)
            after = path.stat()
            if f'{after.st_size}:{after.st_mtime_ns}' != signature or len(data) != stat.st_size:
                continue
            digest = hashlib.sha256(data).hexdigest()
            account = upload_path.split('/')[0].strip().upper()
            receipt = None
            if archive and row[2] == digest and row[6]:
                receipt = upload_receipt(json.loads(row[6]), account)
            else:
                result = upload(upload_path, data)
                if archive:
                    receipt = upload_receipt(result, account)
            confirmed = True
            db.execute('UPDATE files SET digest=?,status=?,error=NULL,attempts=0,retry_at=0,receipt=? WHERE path=?',
                       (digest, 'UPLOADED', json.dumps(receipt) if receipt else None, relative))
            db.commit()
            if archive:
                def record_copy(location):
                    db.execute('UPDATE files SET archived_path=? WHERE path=?', (location, relative))
                    db.commit()
                archived = archive_uploaded(root, path, data, signature, account, previous=row[7], record_copy=record_copy)
                db.execute("UPDATE files SET status='ARCHIVED',archived_path=?,error=NULL WHERE path=?", (archived, relative))
                db.commit()
        except (OSError, ValueError, urllib.error.HTTPError) as exc:
            error = failure_message(exc) if archive else ('HTTP ' + str(exc.code) if isinstance(exc, urllib.error.HTTPError) else type(exc).__name__)
            db.execute('UPDATE files SET status=?,attempts=COALESCE(attempts,0)+1,retry_at=?,error=? WHERE path=?',
                       ('ARCHIVE_RETRY' if confirmed and archive else 'RETRY', now + min(3600, 60 * 2 ** min((row[4] or 0) if row else 0, 6)), error, relative))
            db.commit()
            if isinstance(exc, urllib.error.HTTPError) and exc.code in (401, 403, 404, 429, 503):
                break
    if archive:
        report_error = write_upload_report(root, db)
        if report_error:
            summary['report_error'] = report_error
    return summary


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
            for path, error in db.execute("SELECT path,error FROM files WHERE status IN ('RETRY','ARCHIVE_RETRY') LIMIT 20"):
                print(path, error)
            if (state / 'last-run.json').exists():
                print((state / 'last-run.json').read_text())
        else:
            try:
                client.refresh()
                summary = scan(Path(ROOT), db, client.upload, archive=True)
                retries = db.execute("SELECT count(*) FROM files WHERE status IN ('RETRY','ARCHIVE_RETRY')").fetchone()[0]
                result = {'at': time.time(), 'status': 'RETRY_PENDING' if retries else ('REPORT_NEEDS_ATTENTION' if summary.get('report_error') else 'PASS_COMPLETE'), 'retry_files': retries, **summary}
            except Exception as exc:
                result = {'at': time.time(), 'status': 'FAILED', 'error': type(exc).__name__}
                if isinstance(exc, urllib.error.HTTPError):
                    result['http_status'] = exc.code
                raise
            finally:
                (state / 'last-run.json').write_text(json.dumps(result))


if __name__ == '__main__':
    main()
