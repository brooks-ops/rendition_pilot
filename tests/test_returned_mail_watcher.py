import importlib.util
from pathlib import Path
from unittest.mock import Mock
import urllib.error

spec = importlib.util.spec_from_file_location('watcher', Path(__file__).parents[1] / 'tools/returned_mail_watcher/watcher.py')
watcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watcher)


def fixture(tmp_path):
    root = tmp_path / 'Returned Mail'
    folder = root / 'P123'
    folder.mkdir(parents=True)
    path = folder / 'scan.pdf'
    path.write_bytes(b'scan')
    return root, path, watcher.database(tmp_path)


def test_stable_upload_then_skip_and_detect_replaced_scan(tmp_path):
    root, path, db = fixture(tmp_path)
    upload = Mock()
    watcher.scan(root, db, upload, now=0)
    watcher.scan(root, db, upload, now=59)
    upload.assert_not_called()
    watcher.scan(root, db, upload, now=60)
    upload.assert_called_once_with('P123/scan.pdf', b'scan')
    watcher.scan(root, db, upload, now=120)
    assert upload.call_count == 1
    path.write_bytes(b'replaced scan')
    watcher.scan(root, db, upload, now=180)
    watcher.scan(root, db, upload, now=240)
    assert upload.call_count == 2


def test_failed_upload_retries_without_losing_original(tmp_path):
    root, path, db = fixture(tmp_path)
    upload = Mock(side_effect=[OSError('offline'), {'duplicate': True}])
    watcher.scan(root, db, upload, now=0)
    watcher.scan(root, db, upload, now=60)
    watcher.scan(root, db, upload, now=119)
    assert upload.call_count == 1
    watcher.scan(root, db, upload, now=120)
    assert upload.call_count == 2
    assert path.read_bytes() == b'scan'
    assert db.execute('SELECT status,error FROM files').fetchone() == ('UPLOADED', None)


def test_rate_limit_stops_pass_without_recording_response_secrets(tmp_path):
    root, path, db = fixture(tmp_path)
    (path.parent / 'other.pdf').write_bytes(b'other')
    upload = Mock(side_effect=urllib.error.HTTPError('private', 429, 'secret', {}, None))
    watcher.scan(root, db, upload, now=0)
    watcher.scan(root, db, upload, now=60)
    assert upload.call_count == 1
    assert db.execute("SELECT error FROM files WHERE status='RETRY'").fetchone() == ('HTTP 429',)


def test_ignores_nonaccount_folders_and_symlinks(tmp_path):
    root, path, db = fixture(tmp_path)
    (root / 'misc').mkdir()
    (root / 'misc' / 'scan.pdf').write_bytes(b'bad')
    (path.parent / 'linked.pdf').symlink_to(path)
    upload = Mock()
    watcher.scan(root, db, upload, now=0)
    watcher.scan(root, db, upload, now=60)
    assert upload.call_count == 1


def test_refresh_cookie_round_trip(tmp_path, monkeypatch):
    import http.cookiejar
    monkeypatch.setattr(watcher, 'protect', lambda data, decrypt=False: data)
    client = watcher.Client(tmp_path)
    client.jar.set_cookie(http.cookiejar.Cookie(0, 'session', 'secret', None, False,
        'www.renditionpilot.com', False, False, '/api/auth', True, True, None, True, None, None, {'HttpOnly': None}))
    client.save()
    restored = watcher.Client(tmp_path)
    restored.post = Mock(return_value={'access_token': 'access'})
    restored.refresh()
    assert list(restored.jar)[0].value == 'secret'
    assert restored.token == 'access'
