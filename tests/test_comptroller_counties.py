from __future__ import annotations

import pytest

from app.comptroller import counties


def test_get_county_code_known_county():
    assert counties.get_county_code("Lubbock") == "152"
    assert counties.get_county_code("lubbock") == "152"
    assert counties.get_county_code("  LUBBOCK  ") == "152"


def test_get_county_code_unknown_county_returns_none():
    assert counties.get_county_code("Not A Real County") is None


def test_get_monitored_counties_defaults_to_lubbock(monkeypatch):
    monkeypatch.delenv("COMPTROLLER_MONITORED_COUNTIES", raising=False)
    assert counties.get_monitored_counties() == ["Lubbock"]


def test_get_monitored_counties_parses_comma_separated_list(monkeypatch):
    monkeypatch.setenv("COMPTROLLER_MONITORED_COUNTIES", "Lubbock")
    assert counties.get_monitored_counties() == ["Lubbock"]


def test_get_monitored_counties_rejects_unknown_county_name(monkeypatch):
    monkeypatch.setenv("COMPTROLLER_MONITORED_COUNTIES", "Lubbock,Atlantis")
    with pytest.raises(ValueError):
        counties.get_monitored_counties()


def test_district_slug_defaults_to_convention(monkeypatch):
    monkeypatch.delenv("COMPTROLLER_DISTRICT_SLUG__LUBBOCK", raising=False)
    assert counties.get_district_slug_for_county("Lubbock") == "lubbock-cad"


def test_district_slug_override_via_env(monkeypatch):
    monkeypatch.setenv("COMPTROLLER_DISTRICT_SLUG__LUBBOCK", "custom-slug")
    assert counties.get_district_slug_for_county("Lubbock") == "custom-slug"
