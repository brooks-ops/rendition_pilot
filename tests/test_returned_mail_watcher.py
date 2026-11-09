import importlib.util
from pathlib import Path
from unittest.mock import Mock
import urllib.error
import pytest

spec = importlib.util.spec_from_file_location('watcher', Path(__file__).parents[1] / 'tools/returned_mail_watcher/watcher.py')
watcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watcher)


@pytest.mark.parametrize('filename,account', [('P105330.pdf', 'P105330'), ('p304822.PDF','P304822'), ('000123.pdf','000123')])
def test_flat_account_pdfs_upload_without_creating_folders(tmp_path, filename, account):
    root = tmp_path / 'Returned Mail'
    root.mkdir()
    original = root / filename
    original.write_bytes(b'original PDF')
    db = watcher.database(tmp_path)
    upload = Mock()
    assert watcher.scan(root, db, upload, now=0) == {'eligible_files': 1}
    upload.assert_not_called()
    watcher.scan(root, db, upload, now=60)
    upload.assert_called_once_with(account + '/' + filename, b'original PDF')
    watcher.scan(root, db, upload, now=120)
    assert upload.call_count == 1
    assert list(root.iterdir()) == [original]
    assert original.read_bytes() == b'original PDF'
    assert db.execute('SELECT path,status FROM files').fetchone() == (filename, 'UPLOADED')


def test_flat_and_nested_progress_do_not_collide(tmp_path):
    root, nested, db = fixture(tmp_path)
    nested.rename(nested.parent / 'P123.pdf')
    (root / 'P123.pdf').write_bytes(b'flat')
    upload = Mock()
    watcher.scan(root, db, upload, now=0)
    watcher.scan(root, db, upload, now=60)
    assert upload.call_count == 2
    assert {r[0] for r in db.execute('SELECT path FROM files')} == {'P123.pdf', 'P123/P123.pdf'}


def test_unrecognized_flat_names_are_not_guessed_and_no_files_reported(tmp_path):
    root = tmp_path / 'Returned Mail'
    root.mkdir()
    for name in ('invoice.pdf','P123-extra.pdf','P123.txt','P123.pdf.exe'):
        (root / name).write_bytes(b'unrecognized')
    upload = Mock()
    assert watcher.scan(root, watcher.database(tmp_path), upload, now=0) == {'eligible_files': 0}
    upload.assert_not_called()


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
