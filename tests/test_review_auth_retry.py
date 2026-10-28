"""Transient auth transport errors must not become unhandled review failures."""
from unittest.mock import Mock
import pytest
import requests
from fastapi import HTTPException
from fastapi.testclient import TestClient
from backend import main


def configure(monkeypatch, effects):
    monkeypatch.setattr(main, 'get_supabase_config', lambda: ('https://example.test', 'anon'))
    get = Mock(side_effect=effects)
    monkeypatch.setattr(main.requests, 'get', get)
    return get


def test_dropped_tls_verification_retries_with_certificate_checks_enabled(monkeypatch):
    response = Mock(status_code=200)
    response.json.return_value = {'id': 'verified-user'}
    get = configure(monkeypatch, [requests.exceptions.SSLError('EOF'), response])
    assert main.get_supabase_user('token') == {'id': 'verified-user'}
    assert get.call_count == 2
    assert all(call.kwargs.get('verify', True) for call in get.call_args_list)


@pytest.mark.parametrize('failure', [requests.exceptions.SSLError('EOF'), requests.Timeout('timeout'), Mock(status_code=503)])
def test_persistent_outage_returns_actionable_503_before_review(monkeypatch, failure):
    get = configure(monkeypatch, [failure, failure])
    pipeline = Mock(side_effect=AssertionError('Must authenticate before review'))
    monkeypatch.setattr(main, 'run_pipeline_from_upload', pipeline)
    response = TestClient(main.app).post('/api/review/run', headers={'Authorization':'Bearer token'}, json={})
    assert response.status_code == 503
    assert 'try Run Review again' in response.json()['detail']
    assert get.call_count == 2
    pipeline.assert_not_called()


def test_invalid_token_is_rejected_without_retry(monkeypatch):
    response = Mock(status_code=401)
    response.json.return_value = {'msg': 'Invalid JWT'}
    get = configure(monkeypatch, [response])
    with pytest.raises(HTTPException) as exc:
        main.get_supabase_user('bad-token')
    assert exc.value.status_code == 401
    assert get.call_count == 1
