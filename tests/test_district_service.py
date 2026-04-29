from app.district_service import (
    DistrictContext,
    DistrictServiceError,
    link_user_to_district,
    resolve_district_for_user,
    verify_supabase_district_setup,
)


def test_resolve_district_for_user_prefers_user_id(monkeypatch):
    calls = []

    def fake_request(method, url, headers, params=None, json_payload=None):
        calls.append({"params": params, "json": json_payload})
        return [
            {
                "district_id": "lubbock-id",
                "user_id": "user-123",
                "email": "bbarrett@lubbockcad.org",
                "districts": {
                    "id": "lubbock-id",
                    "name": "Lubbock Central Appraisal District",
                    "slug": "lubbock-cad",
                    "domain": "lubbockcad.org",
                },
            }
        ]

    monkeypatch.setattr("app.district_service._request_json", fake_request)

    result = resolve_district_for_user(
        supabase_url="https://example.supabase.co",
        anon_key="anon",
        access_token="token",
        email="bbarrett@lubbockcad.org",
        user_id="user-123",
        service_role_key="service-role",
    )

    assert isinstance(result, DistrictContext)
    assert result.district_slug == "lubbock-cad"
    assert calls[0]["params"]["user_id"] == "eq.user-123"


def test_resolve_district_for_user_falls_back_to_email_and_backfills_user_id(monkeypatch):
    responses = [
        [],
        [
            {
                "district_id": "dallam-id",
                "user_id": None,
                "email": "cbanister@dallamcad.org",
                "districts": {
                    "id": "dallam-id",
                    "name": "Dallam County Appraisal District",
                    "slug": "dallam-cad",
                    "domain": "dallamcad.org",
                },
            }
        ],
    ]
    link_calls = []

    def fake_request(method, url, headers, params=None, json_payload=None):
        return responses.pop(0)

    def fake_link(**kwargs):
        link_calls.append(kwargs)

    monkeypatch.setattr("app.district_service._request_json", fake_request)
    monkeypatch.setattr("app.district_service.link_user_to_district", fake_link)

    result = resolve_district_for_user(
        supabase_url="https://example.supabase.co",
        anon_key="anon",
        access_token="token",
        email="cbanister@dallamcad.org",
        user_id="user-999",
        service_role_key="service-role",
    )

    assert result is not None
    assert result.district_slug == "dallam-cad"
    assert result.user_id == "user-999"
    assert link_calls[0]["district_id"] == "dallam-id"
    assert link_calls[0]["email"] == "cbanister@dallamcad.org"


def test_resolve_district_for_user_returns_none_for_unlinked_user(monkeypatch):
    def fake_request(method, url, headers, params=None, json_payload=None):
        return []

    monkeypatch.setattr("app.district_service._request_json", fake_request)

    result = resolve_district_for_user(
        supabase_url="https://example.supabase.co",
        anon_key="anon",
        access_token="token",
        email="nobody@example.org",
        user_id="user-missing",
        service_role_key="service-role",
    )

    assert result is None


def test_link_user_to_district_rejects_cross_district_email(monkeypatch):
    existing = DistrictContext(
        district_id="other-district",
        district_slug="other-cad",
        district_name="Other CAD",
        email="user@example.org",
        user_id="user-1",
    )

    def fake_fetch_membership(*args, **kwargs):
        if kwargs.get("email"):
            return existing
        return None

    monkeypatch.setattr("app.district_service._fetch_membership", fake_fetch_membership)

    try:
        link_user_to_district(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            district_id="target-district",
            email="user@example.org",
            user_id="user-1",
        )
    except RuntimeError as exc:
        assert "different district" in str(exc)
    else:
        raise AssertionError("Expected link_user_to_district to reject cross-district reassignment.")


def test_verify_supabase_district_setup_checks_both_tables(monkeypatch):
    calls = []

    def fake_request(method, url, headers, params=None, json_payload=None):
        calls.append(url)
        return []

    monkeypatch.setattr("app.district_service._request_json", fake_request)

    verify_supabase_district_setup(
        supabase_url="https://example.supabase.co",
        service_role_key="service-role",
    )

    assert calls == [
        "https://example.supabase.co/rest/v1/districts",
        "https://example.supabase.co/rest/v1/district_users",
    ]


def test_verify_supabase_district_setup_rewrites_missing_table_errors(monkeypatch):
    def fake_request(method, url, headers, params=None, json_payload=None):
        raise DistrictServiceError("relation \"public.district_users\" does not exist")

    monkeypatch.setattr("app.district_service._request_json", fake_request)

    try:
        verify_supabase_district_setup(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
        )
    except DistrictServiceError as exc:
        assert "Run `supabase/migrations/20260429_multi_district_renditions.sql`" in str(exc)
    else:
        raise AssertionError("Expected verify_supabase_district_setup to surface a setup migration error.")
