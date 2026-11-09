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


def receipt(account='P123'):
    return {'account': account, 'document_id': '11111111-1111-4111-8111-111111111111',
            'item_id': '22222222-2222-4222-8222-222222222222', 'duplicate': True}


def archive_fixture(tmp_path):
    root = tmp_path / 'Returned Mail'
    root.mkdir()
    path = root / 'P123.pdf'
    path.write_bytes(b'scan')
    db = watcher.database(tmp_path)
    upload = Mock(return_value=receipt())
    watcher.scan(root, db, upload, now=0, archive=True)
    return root, path, db, upload


def test_confirmed_upload_archived_and_not_uploaded_again(tmp_path):
    root, path, db, upload = archive_fixture(tmp_path)
    watcher.scan(root, db, upload, now=60, archive=True)
    assert not path.exists()
    assert (root / 'Uploaded/P123.pdf').read_bytes() == b'scan'
    assert db.execute('SELECT status FROM files').fetchone()[0] == 'ARCHIVED'
    assert 'Address still requires appraiser review' in (root / '_Upload status.csv').read_text('utf-8-sig')
    watcher.scan(root, db, upload, now=120, archive=True)
    upload.assert_called_once()


@pytest.mark.parametrize('result', [{}, {'account': 'P999'}, {'account': 'P123', 'document_id': 'bad', 'item_id': 'bad'}])
def test_missing_server_receipt_keeps_original(tmp_path, result):
    root, path, db, upload = archive_fixture(tmp_path)
    upload.return_value = result
    watcher.scan(root, db, upload, now=60, archive=True)
    assert path.read_bytes() == b'scan'
    assert not (root / 'Uploaded').exists()
    assert db.execute('SELECT status FROM files').fetchone()[0] == 'RETRY'


def test_server_validation_failure_is_readable_and_original_kept(tmp_path):
    import io
    root, path, db, upload = archive_fixture(tmp_path)
    upload.side_effect = urllib.error.HTTPError('private', 400, 'Bad Request', {}, io.BytesIO(b'{"detail":"Account not found in CRS."}'))
    watcher.scan(root, db, upload, now=60, archive=True)
    assert path.exists()
    assert 'HTTP 400: Account not found in CRS.' in (root / '_Upload status.csv').read_text('utf-8-sig')


def test_old_uploaded_progress_reconfirms_server_before_archiving(tmp_path):
    root, path, db, upload = archive_fixture(tmp_path)
    db.execute("UPDATE files SET status='UPLOADED'")
    db.commit()
    watcher.scan(root, db, upload, now=60, archive=True)
    upload.assert_called_once()
    assert not path.exists()


def test_new_scan_same_account_never_overwrites_prior_pdf(tmp_path):
    root, path, db, upload = archive_fixture(tmp_path)
    watcher.scan(root, db, upload, now=60, archive=True)
    path.write_bytes(b'new return')
    watcher.scan(root, db, upload, now=120, archive=True)
    watcher.scan(root, db, upload, now=180, archive=True)
    assert {p.read_bytes() for p in (root / 'Uploaded').iterdir()} == {b'scan', b'new return'}
    assert upload.call_count == 2
    assert not path.exists()


def test_scanner_changes_during_upload_preserve_original(tmp_path):
    root, path, db, upload = archive_fixture(tmp_path)
    def changed(*args):
        path.write_bytes(b'scanner still writing')
        return receipt()
    upload.side_effect = changed
    watcher.scan(root, db, upload, now=60, archive=True)
    assert path.read_bytes() == b'scanner still writing'
    assert not (root / 'Uploaded').exists()
    assert db.execute('SELECT status FROM files').fetchone()[0] == 'ARCHIVE_RETRY'


def test_delete_failure_recovers_copy_without_reupload(tmp_path, monkeypatch):
    root, path, db, upload = archive_fixture(tmp_path)
    original_unlink = Path.unlink
    def deny_source(self, *args, **kwargs):
        if self == path:
            raise PermissionError('scanner lock')
        return original_unlink(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', deny_source)
    watcher.scan(root, db, upload, now=60, archive=True)
    assert path.exists()
    assert db.execute('SELECT status FROM files').fetchone()[0] == 'ARCHIVE_RETRY'
    monkeypatch.setattr(Path, 'unlink', original_unlink)
    watcher.scan(root, db, upload, now=120, archive=True)
    assert not path.exists()
    assert len(list((root / 'Uploaded').iterdir())) == 1
    upload.assert_called_once()


def test_unwritable_archive_retries_from_saved_receipt(tmp_path):
    root, path, db, upload = archive_fixture(tmp_path)
    (root / 'Uploaded').write_text('folder blocked')
    watcher.scan(root, db, upload, now=60, archive=True)
    assert path.exists()
    (root / 'Uploaded').unlink()
    watcher.scan(root, db, upload, now=120, archive=True)
    assert not path.exists()
    upload.assert_called_once()


def test_removed_retry_not_counted_and_unknown_filename_reported(tmp_path):
    root, path, db, upload = archive_fixture(tmp_path)
    db.execute("UPDATE files SET status='RETRY',error='old error'")
    db.commit()
    path.unlink()
    (root / '=bad.pdf').write_bytes(b'bad filename')
    watcher.scan(root, db, upload, now=60, archive=True)
    assert db.execute('SELECT status FROM files').fetchone()[0] == 'NOT_IN_INBOX'
    report = (root / '_Upload status.csv').read_text('utf-8-sig')
    assert "'=bad.pdf" in report
    assert 'Filename must be the exact account number' in report


def test_existing_progress_schema_upgraded_without_losing_rows(tmp_path):
    import sqlite3
    db = sqlite3.connect(tmp_path / 'progress.sqlite')
    db.execute('CREATE TABLE files(path TEXT PRIMARY KEY,signature TEXT,stable_since REAL,digest TEXT,status TEXT,attempts INTEGER DEFAULT 0,retry_at REAL DEFAULT 0,error TEXT)')
    db.execute("INSERT INTO files(path,status) VALUES('P123.pdf','UPLOADED')")
    db.commit()
    db.close()
    upgraded = watcher.database(tmp_path)
    assert upgraded.execute('SELECT path,status,receipt,archived_path FROM files').fetchone() == ('P123.pdf','UPLOADED',None,None)
