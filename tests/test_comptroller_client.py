from __future__ import annotations

import pytest

from app.comptroller import client as comptroller_client


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text="", headers=None):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text
        self.headers = headers or {}

    def json(self):
        if self._json_data is None:
            raise ValueError("no json")
        return self._json_data


def _active_row(tp="17512000001", loc="1", **overrides):
    row = {
        "tp_number": tp,
        "tp_name": "SAMPLE TAXPAYER INC",
        "tp_address": "100 MAIN ST",
        "tp_city": "LUBBOCK",
        "tp_state": "TX",
        "tp_zip": "79401",
        "loc_number": loc,
        "loc_name": "SAMPLE LOCATION",
        "address_text": "100 MAIN ST",
        "loc_city": "LUBBOCK",
        "loc_state": "TX",
        "loc_zip": "79401",
        "loc_county": "152",
        "permit_date": "2010-01-01T00:00:00.000",
        "out_of_business_date": None,
    }
    row.update(overrides)
    return row


def test_fetch_derives_active_status_when_no_out_of_business_date(monkeypatch):
    def fake_get(url, headers=None, params=None, timeout=None):
        return FakeResponse(json_data=[_active_row()], headers={"Last-Modified": "Mon, 17 Aug 2026 15:46:22 GMT"})

    monkeypatch.setattr(comptroller_client.requests, "get", fake_get)

    result = comptroller_client.fetch_county_permits("152")

    assert len(result.records) == 1
    record = result.records[0]
    assert record.current_status == "ACTIVE"
    assert record.permit_end_date is None
    assert result.source_data_date is not None


def test_fetch_derives_inactive_status_from_out_of_business_date(monkeypatch):
    def fake_get(url, headers=None, params=None, timeout=None):
        return FakeResponse(json_data=[_active_row(out_of_business_date="2026-03-02T00:00:00.000")])

    monkeypatch.setattr(comptroller_client.requests, "get", fake_get)

    result = comptroller_client.fetch_county_permits("152")

    record = result.records[0]
    assert record.current_status == "INACTIVE"
    assert record.permit_end_date.isoformat() == "2026-03-02"


def test_fetch_raises_on_http_error(monkeypatch):
    def fake_get(url, headers=None, params=None, timeout=None):
        return FakeResponse(status_code=500, text="internal error")

    monkeypatch.setattr(comptroller_client.requests, "get", fake_get)

    with pytest.raises(comptroller_client.ComptrollerClientError):
        comptroller_client.fetch_county_permits("152")


def test_fetch_raises_on_malformed_json(monkeypatch):
    def fake_get(url, headers=None, params=None, timeout=None):
        return FakeResponse(json_data=None)

    monkeypatch.setattr(comptroller_client.requests, "get", fake_get)

    with pytest.raises(comptroller_client.ComptrollerClientError):
        comptroller_client.fetch_county_permits("152")


def test_fetch_raises_on_unexpected_shape(monkeypatch):
    def fake_get(url, headers=None, params=None, timeout=None):
        return FakeResponse(json_data={"not": "a list"})

    monkeypatch.setattr(comptroller_client.requests, "get", fake_get)

    with pytest.raises(comptroller_client.ComptrollerClientError):
        comptroller_client.fetch_county_permits("152")


def test_fetch_skips_rows_missing_identifiers(monkeypatch):
    def fake_get(url, headers=None, params=None, timeout=None):
        return FakeResponse(json_data=[_active_row(), {"tp_name": "no ids"}])

    monkeypatch.setattr(comptroller_client.requests, "get", fake_get)

    result = comptroller_client.fetch_county_permits("152")

    assert len(result.records) == 1
    assert result.skipped_row_count == 1


def test_fetch_dedupes_duplicate_rows_last_write_wins(monkeypatch):
    def fake_get(url, headers=None, params=None, timeout=None):
        return FakeResponse(
            json_data=[
                _active_row(),
                _active_row(out_of_business_date="2026-01-01T00:00:00.000"),
            ]
        )

    monkeypatch.setattr(comptroller_client.requests, "get", fake_get)

    result = comptroller_client.fetch_county_permits("152")

    assert len(result.records) == 1
    assert result.records[0].current_status == "INACTIVE"


def test_fetch_paginates_full_pages(monkeypatch):
    calls = []
    page_size = 2
    first_page = [_active_row(tp="1", loc="1"), _active_row(tp="2", loc="1")]
    second_page = [_active_row(tp="3", loc="1")]

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append(params["$offset"])
        if params["$offset"] == 0:
            return FakeResponse(json_data=first_page)
        return FakeResponse(json_data=second_page)

    monkeypatch.setattr(comptroller_client.requests, "get", fake_get)

    result = comptroller_client.fetch_county_permits("152", page_size=page_size)

    assert calls == [0, 2]
    assert len(result.records) == 3
