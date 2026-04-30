from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Any

import requests


class DistrictServiceError(RuntimeError):
    pass


DISTRICT_SETUP_MIGRATION = "supabase/migrations/20260429_multi_district_renditions.sql"


@dataclass(frozen=True)
class DistrictContext:
    district_id: str
    district_slug: str
    district_name: str
    email: str
    user_id: str | None = None
    domain: str | None = None
    role: str = "member"

    def to_session_dict(self) -> dict[str, str | None]:
        return asdict(self)


def normalize_email(email: str) -> str:
    return str(email or "").strip().lower()


def infer_domain_from_email(email: str) -> str | None:
    normalized = normalize_email(email)
    if "@" not in normalized:
        return None
    return normalized.split("@", 1)[1] or None


def slugify_district_slug(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return cleaned


def slugify_district_name(name: str) -> str:
    slug = slugify_district_slug(name)
    if slug.endswith("-central-appraisal-district"):
        slug = slug.removesuffix("-central-appraisal-district") + "-cad"
    elif slug.endswith("-county-appraisal-district"):
        slug = slug.removesuffix("-county-appraisal-district") + "-cad"
    elif slug.endswith("-appraisal-district"):
        slug = slug.removesuffix("-appraisal-district") + "-cad"
    return slug or "district"


def _extract_error_message(payload: Any) -> str:
    if isinstance(payload, dict):
        return str(
            payload.get("message")
            or payload.get("msg")
            or payload.get("error")
            or payload.get("error_description")
            or payload
        )
    return str(payload)


def _rewrite_district_error(message: str) -> str:
    lowered = str(message or "").lower()
    if "district_users" in lowered or "districts" in lowered:
        if "role" in lowered and "does not exist" in lowered:
            return (
                "Supabase district user roles are missing. Run "
                "`supabase/migrations/20260430_district_user_roles.sql` in the Supabase SQL editor, then try again."
            )
        if (
            "does not exist" in lowered
            or "could not find the table" in lowered
            or "schema cache" in lowered
        ):
            return (
                "Supabase district tables are missing. Run "
                f"`{DISTRICT_SETUP_MIGRATION}` in the Supabase SQL editor, then try again."
            )
        if "permission denied" in lowered or "row-level security" in lowered:
            return (
                "Supabase rejected access to the district tables. Verify the app is using "
                "the service role key and rerun the district setup migration."
            )
    return message


def _request_json(
    method: str,
    url: str,
    headers: dict[str, str],
    *,
    params: dict[str, Any] | None = None,
    json_payload: Any = None,
) -> Any:
    response = requests.request(
        method=method,
        url=url,
        headers=headers,
        params=params,
        json=json_payload,
        timeout=20,
    )

    try:
        payload = response.json()
    except ValueError:
        payload = response.text

    if response.status_code >= 400:
        raise DistrictServiceError(_rewrite_district_error(_extract_error_message(payload)))

    return payload


def _postgrest_headers(api_key: str, access_token: str | None = None, prefer: str | None = None) -> dict[str, str]:
    token = access_token or api_key
    headers = {
        "apikey": api_key,
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def _get_query_headers(
    *,
    anon_key: str,
    access_token: str | None,
    service_role_key: str | None,
) -> dict[str, str]:
    if service_role_key:
        return _postgrest_headers(service_role_key)
    if anon_key and access_token:
        return _postgrest_headers(anon_key, access_token=access_token)
    raise DistrictServiceError("District lookup is not configured. Set SUPABASE_SERVICE_ROLE_KEY.")


def _parse_membership_row(row: dict[str, Any]) -> DistrictContext | None:
    district = row.get("districts") or {}
    district_id = str(row.get("district_id") or district.get("id") or "").strip()
    district_slug = str(district.get("slug") or "").strip()
    district_name = str(district.get("name") or "").strip()
    email = normalize_email(str(row.get("email") or ""))
    if not district_id or not district_slug or not district_name or not email:
        return None
    user_id = str(row.get("user_id") or "").strip() or None
    domain = str(district.get("domain") or "").strip() or None
    role = str(row.get("role") or "member").strip().lower() or "member"
    return DistrictContext(
        district_id=district_id,
        district_slug=district_slug,
        district_name=district_name,
        email=email,
        user_id=user_id,
        domain=domain,
        role=role,
    )


def _fetch_membership(
    supabase_url: str,
    headers: dict[str, str],
    *,
    user_id: str | None = None,
    email: str | None = None,
) -> DistrictContext | None:
    params: dict[str, Any] = {
        "select": "district_id,user_id,email,role,districts(id,name,slug,domain)",
        "limit": "1",
    }
    if user_id:
        params["user_id"] = f"eq.{user_id}"
    elif email:
        params["email"] = f"eq.{normalize_email(email)}"
    else:
        return None

    rows = _request_json(
        "GET",
        f"{supabase_url.rstrip('/')}/rest/v1/district_users",
        headers,
        params=params,
    )
    if not rows:
        return None
    return _parse_membership_row(rows[0])


def verify_supabase_district_setup(
    *,
    supabase_url: str,
    service_role_key: str,
) -> None:
    headers = _postgrest_headers(service_role_key)
    for table_name in ("districts", "district_users"):
        try:
            _request_json(
                "GET",
                f"{supabase_url.rstrip('/')}/rest/v1/{table_name}",
                headers,
                params={"select": "*", "limit": "1"},
            )
        except DistrictServiceError as exc:
            raise DistrictServiceError(_rewrite_district_error(str(exc))) from exc


def create_or_update_district(
    *,
    supabase_url: str,
    service_role_key: str,
    name: str,
    slug: str,
    domain: str | None = None,
) -> DistrictContext:
    normalized_slug = slugify_district_slug(slug) or slugify_district_name(name)
    payload = {
        "name": str(name or "").strip(),
        "slug": normalized_slug,
        "domain": (str(domain or "").strip().lower() or None),
    }
    rows = _request_json(
        "POST",
        f"{supabase_url.rstrip('/')}/rest/v1/districts",
        _postgrest_headers(
            service_role_key,
            prefer="resolution=merge-duplicates,return=representation",
        ),
        params={"on_conflict": "slug"},
        json_payload=payload,
    )
    row = rows[0] if isinstance(rows, list) else rows
    district_id = str(row.get("id") or "").strip()
    if not district_id:
        raise DistrictServiceError("District upsert did not return an id.")
    return DistrictContext(
        district_id=district_id,
        district_slug=str(row.get("slug") or normalized_slug),
        district_name=str(row.get("name") or payload["name"]),
        email="",
        domain=str(row.get("domain") or payload["domain"] or "").strip() or None,
    )


def find_district_by_domain(
    *,
    supabase_url: str,
    service_role_key: str,
    domain: str,
) -> DistrictContext | None:
    normalized_domain = str(domain or "").strip().lower()
    if not normalized_domain:
        return None
    rows = _request_json(
        "GET",
        f"{supabase_url.rstrip('/')}/rest/v1/districts",
        _postgrest_headers(service_role_key),
        params={
            "select": "id,name,slug,domain",
            "domain": f"eq.{normalized_domain}",
            "limit": "1",
        },
    )
    if not rows:
        return None
    row = rows[0]
    district_id = str(row.get("id") or "").strip()
    district_slug = str(row.get("slug") or "").strip()
    district_name = str(row.get("name") or "").strip()
    if not district_id or not district_slug or not district_name:
        return None
    return DistrictContext(
        district_id=district_id,
        district_slug=district_slug,
        district_name=district_name,
        email="",
        domain=str(row.get("domain") or normalized_domain),
    )


def link_user_to_district(
    *,
    supabase_url: str,
    service_role_key: str,
    district_id: str,
    email: str,
    user_id: str | None = None,
    role: str = "member",
) -> None:
    normalized_email = normalize_email(email)
    normalized_role = str(role or "member").strip().lower()
    if normalized_role not in {"admin", "member"}:
        normalized_role = "member"
    headers = _postgrest_headers(service_role_key)

    existing_by_email = _fetch_membership(
        supabase_url,
        headers,
        email=normalized_email,
    )
    if existing_by_email and existing_by_email.district_id != district_id:
        raise DistrictServiceError("That email is already linked to a different district.")

    if user_id:
        existing_by_user = _fetch_membership(
            supabase_url,
            headers,
            user_id=user_id,
        )
        if existing_by_user and existing_by_user.district_id != district_id:
            raise DistrictServiceError("That Supabase user is already linked to a different district.")

    payload = {
        "district_id": district_id,
        "user_id": user_id,
        "email": normalized_email,
        "role": normalized_role,
    }
    _request_json(
        "POST",
        f"{supabase_url.rstrip('/')}/rest/v1/district_users",
        _postgrest_headers(
            service_role_key,
            prefer="resolution=merge-duplicates,return=representation",
        ),
        params={"on_conflict": "email"},
        json_payload=payload,
    )


def resolve_district_for_user(
    *,
    supabase_url: str,
    anon_key: str,
    access_token: str | None,
    email: str,
    user_id: str | None = None,
    service_role_key: str | None = None,
) -> DistrictContext | None:
    headers = _get_query_headers(
        anon_key=anon_key,
        access_token=access_token,
        service_role_key=service_role_key,
    )

    membership = None
    if user_id:
        membership = _fetch_membership(
            supabase_url,
            headers,
            user_id=user_id,
        )

    if membership is None and email:
        membership = _fetch_membership(
            supabase_url,
            headers,
            email=email,
        )
        if membership and user_id and service_role_key and membership.user_id != user_id:
            link_user_to_district(
                supabase_url=supabase_url,
                service_role_key=service_role_key,
                district_id=membership.district_id,
                email=email,
                user_id=user_id,
            )
            membership = DistrictContext(
                district_id=membership.district_id,
                district_slug=membership.district_slug,
                district_name=membership.district_name,
                email=membership.email,
                user_id=user_id,
                domain=membership.domain,
                role=membership.role,
            )

    return membership


def get_invited_district_user(
    *,
    supabase_url: str,
    service_role_key: str,
    email: str,
) -> DistrictContext | None:
    return _fetch_membership(
        supabase_url,
        _postgrest_headers(service_role_key),
        email=normalize_email(email),
    )


def list_district_users(
    *,
    supabase_url: str,
    service_role_key: str,
    district_id: str,
) -> list[dict[str, Any]]:
    rows = _request_json(
        "GET",
        f"{supabase_url.rstrip('/')}/rest/v1/district_users",
        _postgrest_headers(service_role_key),
        params={
            "select": "id,district_id,user_id,email,role,created_at",
            "district_id": f"eq.{district_id}",
            "order": "created_at.asc",
        },
    )
    return rows if isinstance(rows, list) else []
