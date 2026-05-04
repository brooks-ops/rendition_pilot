from __future__ import annotations

import base64
import json
import os
import tempfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Literal

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.arb.arb_analyzer import analyze_arb_evidence, build_summary_text, infer_case_info
from app.arb.arb_models import ARBPacketUpdateRequest, ARBReviewRequest
from app.arb.arb_packet import build_updated_evidence_packet
from app.arb.arb_parser import decode_upload_base64, parse_evidence_packet
from app.arb.arb_ui import arb_page_path
from app.district_service import (
    DistrictContext,
    DistrictServiceError,
    create_or_update_district,
    find_district_by_domain,
    get_invited_district_user,
    infer_domain_from_email,
    link_user_to_district,
    list_district_users,
    normalize_email,
    resolve_district_for_user,
    slugify_district_name,
    slugify_district_slug,
    verify_supabase_district_setup,
)
from app.review_workflow import (
    build_final_review_record,
    get_decision_label,
    get_recommended_value,
    save_review_outputs,
    stamp_reviewed_pdf,
)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")
load_dotenv(PROJECT_ROOT / "app" / ".env", override=True)
DEFAULT_SUPABASE_URL = "https://pzawjgckzcgnfsfuylqy.supabase.co"
DEFAULT_SUPABASE_ANON_KEY = "sb_publishable_q6lNn59Y-kz8lG0cYfJkYw_lL7xElsA"
UNLINKED_DISTRICT_MESSAGE = "Your account is not currently linked to an appraisal district. Please contact an administrator."
ARB_UNAUTHORIZED_MESSAGE = "This email is not authorized for ARB Pilot. Contact the ARB Pilot administrator."
ARB_ALLOWED_EMAILS = {
    "bbarrett@lubbockcar.org",
    "bbarrett@lubbockcad.org",
    "hstewart@lubbockcad.org",
    "evaldez@lubbockcad.org",
    "lcantrell@lubbockcad.org",
    "lsloan@lubbockcad.org",
    "bmilner@lubbockcad.org",
}
TABLE_METADATA = {
    "5_year": {"label": "5 year", "life_years": 5},
    "8_year": {"label": "8 year", "life_years": 8},
    "9_year": {"label": "9 year", "life_years": 9},
    "12_year": {"label": "12 year", "life_years": 12},
}
SECTION_PRESETS = {
    "schedule_a_furniture": {
        "label": "Schedule A - Furniture & Fixtures",
        "schedule": "A",
        "category": "Furniture & Fixtures",
        "default_table": "9_year",
        "entry_mode": "depreciation",
    },
    "schedule_a_machinery": {
        "label": "Schedule A - Machinery & Equipment",
        "schedule": "A",
        "category": "Machinery & Equipment",
        "default_table": "9_year",
        "entry_mode": "depreciation",
    },
    "schedule_b_inventory": {
        "label": "Schedule B - Inventory",
        "schedule": "B",
        "category": "Inventory",
        "default_table": "flat",
        "entry_mode": "flat",
    },
    "freeport_exemption": {
        "label": "Freeport",
        "schedule": "Freeport",
        "category": "Freeport Exemption",
        "default_table": "freeport",
        "entry_mode": "freeport",
    },
    "schedule_c_supplies": {
        "label": "Schedule C - Supplies",
        "schedule": "C",
        "category": "Supplies",
        "default_table": "flat",
        "entry_mode": "flat",
    },
    "schedule_d_vehicles": {
        "label": "Schedule D - Vehicles",
        "schedule": "D",
        "category": "Vehicles",
        "default_table": "9_year",
        "entry_mode": "depreciation",
        "supplemental_flat_label": "Auto Roll Value",
    },
    "schedule_e_computers": {
        "label": "Schedule E - Computers",
        "schedule": "E",
        "category": "Computers",
        "default_table": "5_year",
        "entry_mode": "depreciation",
    },
    "allocation_calculator": {
        "label": "Allocation Calculator",
        "schedule": "Allocation",
        "category": "Allocated Value",
        "default_table": "allocation",
        "entry_mode": "allocation",
    },
    "custom": {
        "label": "Custom",
        "schedule": "Custom",
        "category": "Custom",
        "default_table": "8_year",
        "entry_mode": "depreciation",
    },
}
DEPRECIATION_SCHEDULE_MAP = {
    "5": "5_year",
    "8": "8_year",
    "9": "9_year",
    "12": "12_year",
}


def calculate_combined_total(saved_calculators: list[dict[str, Any]]) -> float:
    return round(sum(float(item.get("section_total", 0.0) or 0.0) for item in saved_calculators), 2)
REVIEW_DECISION_LABELS = {
    "Accepted Recommended Value": "accepted",
    "Adjusted Value": "adjusted",
    "Closed": "closed",
    "No Assets": "no_assets",
}


app = FastAPI(title="Appraisal District Copilot API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:8080",
        "http://127.0.0.1:8080",
        "null",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIR = PROJECT_ROOT / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/frontend", StaticFiles(directory=str(FRONTEND_DIR)), name="frontend")


class CalculateRequest(BaseModel):
    schedule_type: str
    rows: list[dict[str, Any]]
    depreciation_schedule: str
    district_id: str | None = None


class PdfRequest(BaseModel):
    file_name: str
    file_base64: str


class ManualOverrideRequest(BaseModel):
    attachment_total: float | None = None
    good_faith_value: float | None = None
    historical_cost: float | None = None
    acquisition_year: int | None = None
    life_years: int | None = None
    notes: str = ""


class ReviewRunRequest(PdfRequest):
    manual_override: ManualOverrideRequest | None = None
    include_pdf_pages: bool = False
    fast_review: bool = True


class SessionRequest(BaseModel):
    access_token: str


class LoginRequest(BaseModel):
    email: str
    password: str


class SignupRequest(LoginRequest):
    confirm_password: str


class DistrictSetupRequest(SignupRequest):
    district_name: str
    district_slug: str = ""
    admin_email: str


class DistrictInviteRequest(SessionRequest):
    email: str
    role: Literal["admin", "member"] = "member"


class CadDistrictInfoRequest(BaseModel):
    cad_name: str
    display_name: str = ""
    email: str = ""
    phone: str = ""
    address: str = ""
    website: str = ""


class CadUserRequest(BaseModel):
    first_name: str = ""
    last_name: str = ""
    email: str
    role_title: str = ""


class CadAdminRequest(CadUserRequest):
    pass


class CadScheduleRowRequest(BaseModel):
    year_number: int
    depreciation_percent: float


class CadScheduleRequest(BaseModel):
    schedule_name: str
    schedule_type: str = ""
    schedule_years: int
    rows: list[CadScheduleRowRequest]


class CadOnboardingRequest(BaseModel):
    district: CadDistrictInfoRequest
    admin: CadAdminRequest
    users: list[CadUserRequest] = Field(default_factory=list)
    schedules: list[CadScheduleRequest]
    admin_password: str = ""
    admin_confirm_password: str = ""
    district_slug: str = ""


class CadScheduleSaveRequest(BaseModel):
    access_token: str
    schedule: CadScheduleRequest


class ARBAuthRequest(BaseModel):
    access_token: str


class ARBReviewRunRequest(ARBReviewRequest):
    access_token: str


class LockReviewRequest(BaseModel):
    file_name: str
    result: dict[str, Any]
    final_value: float | None
    final_source: str
    appraiser_notes: str = ""
    appraiser_initials: str = ""
    account_number: str = ""
    decision: str = "accepted"
    district_context: dict[str, Any] | None = None
    saved_calculators: list[dict[str, Any]] = Field(default_factory=list)


class SaveReviewRequest(BaseModel):
    file_name: str
    file_base64: str
    result: dict[str, Any]
    final_record: dict[str, Any]
    district_context: dict[str, Any] | None = None


def get_secret(name: str, default: str = "") -> str:
    return os.getenv(name, default) or default


def hydrate_analysis_env() -> None:
    secret_names = [
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
        "OPENAI_VISION_OCR_MODEL",
        "OPENAI_VISION_OCR_TIMEOUT_SECONDS",
        "OPENAI_VISION_OCR_MAX_PAGES",
        "OPENAI_REVIEW_ENABLED",
        "OPENAI_REVIEW_TIMEOUT_SECONDS",
        "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT",
        "AZURE_DOCUMENT_INTELLIGENCE_KEY",
        "AZURE_DOCUMENT_INTELLIGENCE_API_VERSION",
        "AZURE_DOCUMENT_INTELLIGENCE_MODEL_ID",
        "AZURE_DOCUMENT_INTELLIGENCE_TIMEOUT_SECONDS",
        "AZURE_DOCUMENT_INTELLIGENCE_REQUEST_TIMEOUT_SECONDS",
        "AZURE_FORM_RECOGNIZER_ENDPOINT",
        "AZURE_FORM_RECOGNIZER_KEY",
        "GOOGLE_DOCUMENT_AI_PROJECT_ID",
        "GOOGLE_DOCUMENT_AI_LOCATION",
        "GOOGLE_DOCUMENT_AI_PROCESSOR_ID",
        "GOOGLE_DOCUMENT_AI_PROCESSOR_NAME",
        "GOOGLE_DOCUMENT_AI_API_KEY",
        "GOOGLE_DOCUMENT_AI_ACCESS_TOKEN",
        "GOOGLE_VISION_API_KEY",
        "GOOGLE_CLOUD_VISION_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "APP_BASE_URL",
        "SUPABASE_EMAIL_REDIRECT_TO",
        "SUPABASE_URL",
        "SUPABASE_ANON_KEY",
        "SUPABASE_SERVICE_ROLE_KEY",
    ]
    for name in secret_names:
        value = get_secret(name, "")
        if value and not os.getenv(name):
            os.environ[name] = value


def get_supabase_config() -> tuple[str, str]:
    return (
        get_secret("SUPABASE_URL", DEFAULT_SUPABASE_URL).rstrip("/"),
        get_secret("SUPABASE_ANON_KEY", DEFAULT_SUPABASE_ANON_KEY),
    )


def get_supabase_service_role_key() -> str:
    return get_secret("SUPABASE_SERVICE_ROLE_KEY", "")


def get_supabase_email_redirect_url() -> str | None:
    redirect_url = (
        get_secret("SUPABASE_EMAIL_REDIRECT_TO", "")
        or get_secret("APP_BASE_URL", "")
    ).strip()
    return redirect_url or None


def provider_status_snapshot() -> dict[str, Any]:
    return {
        "google_document_ai": {
            "configured": bool(
                get_secret("GOOGLE_DOCUMENT_AI_PROCESSOR_NAME", "")
                or (
                    get_secret("GOOGLE_DOCUMENT_AI_PROJECT_ID", "")
                    and get_secret("GOOGLE_DOCUMENT_AI_LOCATION", "")
                    and get_secret("GOOGLE_DOCUMENT_AI_PROCESSOR_ID", "")
                )
            )
            and bool(get_secret("GOOGLE_DOCUMENT_AI_API_KEY", "") or get_secret("GOOGLE_DOCUMENT_AI_ACCESS_TOKEN", "")),
            "has_api_key": bool(get_secret("GOOGLE_DOCUMENT_AI_API_KEY", "")),
            "has_access_token": bool(get_secret("GOOGLE_DOCUMENT_AI_ACCESS_TOKEN", "")),
        },
        "google_cloud_vision": {
            "configured": bool(get_secret("GOOGLE_VISION_API_KEY", "") or get_secret("GOOGLE_CLOUD_VISION_API_KEY", "")),
        },
        "openai_vision_ocr": {
            "configured": bool(get_secret("OPENAI_API_KEY", "")),
            "model": get_secret("OPENAI_VISION_OCR_MODEL", "") or get_secret("OPENAI_MODEL", "") or "gpt-4.1-mini",
        },
        "azure_document_intelligence": {
            "configured": bool(
                (get_secret("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT", "") or get_secret("AZURE_FORM_RECOGNIZER_ENDPOINT", ""))
                and (get_secret("AZURE_DOCUMENT_INTELLIGENCE_KEY", "") or get_secret("AZURE_FORM_RECOGNIZER_KEY", ""))
            ),
        },
        "openai_review": {
            "configured": bool(get_secret("OPENAI_API_KEY", "")),
            "enabled": get_secret("OPENAI_REVIEW_ENABLED", "").strip().lower() in {"1", "true", "yes"},
            "model": get_secret("OPENAI_MODEL", "") or "gpt-4.1-mini",
        },
    }


def to_jsonable(data: Any) -> Any:
    return json.loads(json.dumps(data, default=str))


def parse_money_input(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def is_zero_value_decision(decision_code: str) -> bool:
    return decision_code in {"closed", "no_assets"}


def _to_float(value: Any) -> float:
    try:
        return round(float(value or 0.0), 2)
    except (TypeError, ValueError):
        return 0.0


def _decode_pdf(file_base64: str) -> bytes:
    try:
        encoded = file_base64.split(",", 1)[-1]
        return base64.b64decode(encoded)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid PDF payload: {exc}") from exc


def _encode_bytes(payload: bytes) -> str:
    return base64.b64encode(payload).decode("ascii")


def _district_to_dict(district: DistrictContext | None) -> dict[str, Any] | None:
    if not district:
        return None
    return {
        "district_id": district.district_id,
        "district_slug": district.district_slug,
        "district_name": district.district_name,
        "district_domain": district.domain,
        "email": district.email,
        "user_id": district.user_id,
        "role": district.role,
    }


def get_authenticated_district_context(access_token: str) -> DistrictContext:
    user = get_supabase_user(access_token)
    district = build_district_context(user, access_token)
    if not district:
        raise HTTPException(status_code=403, detail=UNLINKED_DISTRICT_MESSAGE)
    return district


def require_district_admin(access_token: str) -> DistrictContext:
    district = get_authenticated_district_context(access_token)
    if district.role != "admin":
        raise HTTPException(status_code=403, detail="Only district admins can manage authorized users.")
    return district


def build_district_context(user: dict[str, Any], access_token: str) -> DistrictContext | None:
    supabase_url, anon_key = get_supabase_config()
    service_role_key = get_supabase_service_role_key()
    email = normalize_email(str(user.get("email") or ""))
    user_id = str(user.get("id") or "").strip() or None
    return resolve_district_for_user(
        supabase_url=supabase_url,
        anon_key=anon_key,
        access_token=access_token,
        email=email,
        user_id=user_id,
        service_role_key=service_role_key or None,
    )


def supabase_auth_request(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    supabase_url, anon_key = get_supabase_config()
    if not anon_key:
        raise HTTPException(status_code=500, detail="SUPABASE_ANON_KEY is not configured.")

    response = requests.post(
        f"{supabase_url}/auth/v1/{path.lstrip('/')}",
        headers={
            "apikey": anon_key,
            "Authorization": f"Bearer {anon_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=20,
    )
    try:
        data = response.json()
    except ValueError:
        data = {"message": response.text}
    if response.status_code >= 400:
        message = data.get("msg") or data.get("message") or data.get("error_description") or "Supabase auth request failed."
        raise HTTPException(status_code=response.status_code, detail=str(message))
    return data


def sign_in_with_supabase(email: str, password: str) -> dict[str, Any]:
    return supabase_auth_request(
        "token?grant_type=password",
        {"email": email, "password": password},
    )


def create_supabase_account(email: str, password: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "email": email,
        "password": password,
        "data": {"role": "appraiser", "allowed_app": "rendition_pilot"},
    }
    redirect_url = get_supabase_email_redirect_url()
    if redirect_url:
        payload["email_redirect_to"] = redirect_url
    return supabase_auth_request("signup", payload)


def create_supabase_account_for_app(email: str, password: str, app_name: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "email": email,
        "password": password,
        "data": {"role": "appraiser", "allowed_app": app_name},
    }
    redirect_url = get_supabase_email_redirect_url()
    if redirect_url:
        payload["email_redirect_to"] = redirect_url
    return supabase_auth_request("signup", payload)


def get_supabase_user(access_token: str) -> dict[str, Any]:
    supabase_url, anon_key = get_supabase_config()
    response = requests.get(
        f"{supabase_url}/auth/v1/user",
        headers={"apikey": anon_key, "Authorization": f"Bearer {access_token}"},
        timeout=20,
    )
    try:
        data = response.json()
    except ValueError:
        data = {"message": response.text}
    if response.status_code >= 400:
        message = data.get("msg") or data.get("message") or "Could not restore Supabase session."
        raise HTTPException(status_code=response.status_code, detail=str(message))
    return data


def app_access_headers(prefer: str | None = None) -> dict[str, str]:
    service_role_key = get_supabase_service_role_key()
    if not service_role_key:
        raise HTTPException(status_code=500, detail="SUPABASE_SERVICE_ROLE_KEY is required for ARB Pilot access control.")
    headers = {
        "apikey": service_role_key,
        "Authorization": f"Bearer {service_role_key}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def app_access_request(method: str, path: str, *, params: dict[str, Any] | None = None, payload: Any = None) -> Any:
    supabase_url, _anon_key = get_supabase_config()
    prefer = None
    if method.upper() == "POST":
        prefer = "resolution=merge-duplicates,return=representation"
    elif method.upper() == "PATCH":
        prefer = "return=representation"
    response = requests.request(
        method,
        f"{supabase_url.rstrip('/')}/rest/v1/{path.lstrip('/')}",
        headers=app_access_headers(prefer),
        params=params,
        json=payload,
        timeout=20,
    )
    try:
        data = response.json()
    except ValueError:
        data = response.text
    if response.status_code >= 400:
        message = data.get("message") if isinstance(data, dict) else str(data)
        if "app_access" in str(message) and ("does not exist" in str(message).lower() or "schema cache" in str(message).lower()):
            message = "ARB Pilot access table is missing. Run supabase/migrations/20260430_arb_pilot_access.sql in Supabase."
        raise HTTPException(status_code=response.status_code, detail=message or "App access request failed.")
    return data


def cad_data_request(method: str, path: str, *, params: dict[str, Any] | None = None, payload: Any = None) -> Any:
    supabase_url, _anon_key = get_supabase_config()
    service_role_key = get_supabase_service_role_key()
    if not service_role_key:
        raise HTTPException(status_code=500, detail="SUPABASE_SERVICE_ROLE_KEY is required for CAD onboarding.")
    prefer = None
    if method.upper() == "POST":
        prefer = "return=representation"
    elif method.upper() == "PATCH":
        prefer = "return=representation"
    response = requests.request(
        method,
        f"{supabase_url.rstrip('/')}/rest/v1/{path.lstrip('/')}",
        headers={
            "apikey": service_role_key,
            "Authorization": f"Bearer {service_role_key}",
            "Content-Type": "application/json",
            **({"Prefer": prefer} if prefer else {}),
        },
        params=params,
        json=payload,
        timeout=20,
    )
    try:
        data = response.json()
    except ValueError:
        data = response.text
    if response.status_code >= 400:
        message = data.get("message") if isinstance(data, dict) else str(data)
        if "cad_" in str(message) and ("does not exist" in str(message).lower() or "schema cache" in str(message).lower()):
            message = "CAD onboarding tables are missing. Run supabase/migrations/20260501_cad_onboarding.sql in Supabase."
        raise HTTPException(status_code=response.status_code, detail=message or "CAD onboarding request failed.")
    return data


def normalize_depreciation_factor(value: Any) -> float:
    amount = float(value)
    if amount > 1:
        return round(amount / 100, 6)
    return round(amount, 6)


def validate_cad_schedule_payload(schedule: CadScheduleRequest) -> None:
    if not schedule.schedule_name.strip():
        raise HTTPException(status_code=400, detail="Schedule name is required.")
    if int(schedule.schedule_years or 0) <= 0:
        raise HTTPException(status_code=400, detail="Schedule years must be a positive integer.")
    if not schedule.rows:
        raise HTTPException(status_code=400, detail="Each schedule must have at least one year row.")
    seen_years: set[int] = set()
    for row in schedule.rows:
        if int(row.year_number or 0) <= 0:
            raise HTTPException(status_code=400, detail="Year numbers must be positive integers.")
        if row.year_number in seen_years:
            raise HTTPException(status_code=400, detail="Schedule year rows cannot contain duplicate year numbers.")
        seen_years.add(row.year_number)
        try:
            float(row.depreciation_percent)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Depreciation percentages must be numbers.") from exc
        if float(row.depreciation_percent) < 0:
            raise HTTPException(status_code=400, detail="Depreciation percentages cannot be negative.")


def validate_cad_onboarding_payload(request: CadOnboardingRequest) -> None:
    if not request.district.cad_name.strip():
        raise HTTPException(status_code=400, detail="CAD Name is required.")
    if "@" not in normalize_email(request.admin.email):
        raise HTTPException(status_code=400, detail="Admin email is required.")
    if not request.schedules:
        raise HTTPException(status_code=400, detail="Add at least one depreciation schedule.")
    for schedule in request.schedules:
        validate_cad_schedule_payload(schedule)
    if request.admin_password or request.admin_confirm_password:
        if len(request.admin_password) < 8:
            raise HTTPException(status_code=400, detail="Admin password must be at least 8 characters.")
        if request.admin_password != request.admin_confirm_password:
            raise HTTPException(status_code=400, detail="Admin passwords do not match.")


def fetch_cad_profile_by_district_id(district_id: str) -> dict[str, Any] | None:
    rows = cad_data_request(
        "GET",
        "cad_districts",
        params={
            "select": "*",
            "district_id": f"eq.{district_id}",
            "limit": "1",
        },
    )
    return rows[0] if rows else None


def fetch_cad_schedule(schedule_id: str) -> dict[str, Any] | None:
    rows = cad_data_request(
        "GET",
        "cad_depreciation_schedules",
        params={"select": "*", "id": f"eq.{schedule_id}", "limit": "1"},
    )
    return rows[0] if rows else None


def fetch_cad_schedules_for_district(district_id: str) -> list[dict[str, Any]]:
    cad_profile = fetch_cad_profile_by_district_id(district_id)
    if not cad_profile:
        return []
    schedules = cad_data_request(
        "GET",
        "cad_depreciation_schedules",
        params={
            "select": "*",
            "district_id": f"eq.{cad_profile['id']}",
            "is_active": "eq.true",
            "order": "created_at.asc",
        },
    )
    if not schedules:
        return []
    schedule_ids = ",".join(str(item["id"]) for item in schedules)
    rows = cad_data_request(
        "GET",
        "cad_depreciation_schedule_rows",
        params={
            "select": "*",
            "schedule_id": f"in.({schedule_ids})",
            "order": "year_number.asc",
        },
    )
    rows_by_schedule: dict[str, list[dict[str, Any]]] = {}
    for row in rows or []:
        rows_by_schedule.setdefault(str(row.get("schedule_id")), []).append(row)
    return [
        {
            **schedule,
            "cad_district_id": cad_profile["id"],
            "district_id": cad_profile.get("district_id"),
            "rows": rows_by_schedule.get(str(schedule.get("id")), []),
            "factors": [
                normalize_depreciation_factor(row.get("depreciation_percent"))
                for row in rows_by_schedule.get(str(schedule.get("id")), [])
            ],
        }
        for schedule in schedules
    ]


def create_cad_schedule(cad_district_id: str, schedule: CadScheduleRequest) -> dict[str, Any]:
    validate_cad_schedule_payload(schedule)
    created = cad_data_request(
        "POST",
        "cad_depreciation_schedules",
        payload={
            "district_id": cad_district_id,
            "schedule_name": schedule.schedule_name.strip(),
            "schedule_type": schedule.schedule_type.strip() or schedule.schedule_name.strip(),
            "schedule_years": int(schedule.schedule_years),
            "is_active": True,
        },
    )
    schedule_row = created[0] if isinstance(created, list) else created
    schedule_id = schedule_row["id"]
    row_payload = [
        {
            "schedule_id": schedule_id,
            "year_number": int(row.year_number),
            "depreciation_percent": float(row.depreciation_percent),
        }
        for row in sorted(schedule.rows, key=lambda item: item.year_number)
    ]
    if row_payload:
        cad_data_request("POST", "cad_depreciation_schedule_rows", payload=row_payload)
    return {**schedule_row, "rows": row_payload, "factors": [normalize_depreciation_factor(row["depreciation_percent"]) for row in row_payload]}


def replace_cad_schedule(schedule_id: str, schedule: CadScheduleRequest) -> dict[str, Any]:
    validate_cad_schedule_payload(schedule)
    updated = cad_data_request(
        "PATCH",
        "cad_depreciation_schedules",
        params={"id": f"eq.{schedule_id}"},
        payload={
            "schedule_name": schedule.schedule_name.strip(),
            "schedule_type": schedule.schedule_type.strip() or schedule.schedule_name.strip(),
            "schedule_years": int(schedule.schedule_years),
            "is_active": True,
        },
    )
    cad_data_request("DELETE", "cad_depreciation_schedule_rows", params={"schedule_id": f"eq.{schedule_id}"})
    row_payload = [
        {
            "schedule_id": schedule_id,
            "year_number": int(row.year_number),
            "depreciation_percent": float(row.depreciation_percent),
        }
        for row in sorted(schedule.rows, key=lambda item: item.year_number)
    ]
    if row_payload:
        cad_data_request("POST", "cad_depreciation_schedule_rows", payload=row_payload)
    schedule_row = updated[0] if isinstance(updated, list) and updated else fetch_cad_schedule(schedule_id) or {}
    return {**schedule_row, "rows": row_payload, "factors": [normalize_depreciation_factor(row["depreciation_percent"]) for row in row_payload]}


def seed_arb_access_for_district_user(email: str, user_id: str | None = None, role: str = "member") -> None:
    try:
        app_access_request(
            "POST",
            "app_access",
            params={"on_conflict": "email,app_name"},
            payload={
                "email": normalize_email(email),
                "app_name": "arb_pilot",
                "user_id": user_id,
                "role": "admin" if role == "admin" else "member",
            },
        )
    except HTTPException:
        return


def get_app_access(email: str, app_name: str) -> dict[str, Any] | None:
    normalized_email = normalize_email(email)
    rows = app_access_request(
        "GET",
        "app_access",
        params={
            "select": "id,app_name,email,user_id,role,created_at",
            "email": f"eq.{normalized_email}",
            "app_name": f"eq.{app_name}",
            "limit": "1",
        },
    )
    if rows:
        return rows[0]
    if app_name == "arb_pilot" and normalized_email in ARB_ALLOWED_EMAILS:
        seed_arb_access_email(normalized_email)
        rows = app_access_request(
            "GET",
            "app_access",
            params={
                "select": "id,app_name,email,user_id,role,created_at",
                "email": f"eq.{normalized_email}",
                "app_name": "eq.arb_pilot",
                "limit": "1",
            },
        )
        return rows[0] if rows else None
    return None


def seed_arb_access_email(email: str, user_id: str | None = None) -> None:
    normalized_email = normalize_email(email)
    if normalized_email not in ARB_ALLOWED_EMAILS:
        return
    role = "admin" if normalized_email == "bbarrett@lubbockcar.org" else "member"
    app_access_request(
        "POST",
        "app_access",
        params={"on_conflict": "email,app_name"},
        payload={
            "email": normalized_email,
            "app_name": "arb_pilot",
            "user_id": user_id,
            "role": role,
        },
    )


def link_app_access_user(email: str, app_name: str, user_id: str | None) -> None:
    if not user_id:
        return
    access = get_app_access(email, app_name)
    if not access:
        raise HTTPException(status_code=403, detail=ARB_UNAUTHORIZED_MESSAGE)
    app_access_request(
        "PATCH",
        "app_access",
        params={"id": f"eq.{access['id']}"},
        payload={"user_id": user_id},
    )


def require_arb_access(access_token: str) -> dict[str, Any]:
    user = get_supabase_user(access_token)
    email = normalize_email(str(user.get("email") or ""))
    user_id = str(user.get("id") or "").strip() or None
    access = get_app_access(email, "arb_pilot")
    if not access:
        district = build_district_context(user, access_token)
        if not district:
            raise HTTPException(status_code=403, detail=ARB_UNAUTHORIZED_MESSAGE)
        return {
            "access_token": access_token,
            "user": to_jsonable(user),
            "app_access": {
                "email": email,
                "app_name": "arb_pilot",
                "user_id": user_id,
                "role": district.role,
                "district_id": district.district_id,
            },
        }
    if user_id and access.get("user_id") != user_id:
        link_app_access_user(email, "arb_pilot", user_id)
        access = {**access, "user_id": user_id}
    return {
        "access_token": access_token,
        "user": to_jsonable(user),
        "app_access": to_jsonable(access),
    }


def run_pipeline_from_upload(
    file_name: str,
    file_bytes: bytes,
    manual_override: dict[str, Any] | None = None,
    *,
    fast_review: bool = False,
) -> dict[str, Any]:
    from app.pipeline import run_rendition_pipeline

    hydrate_analysis_env()
    previous_fast_review = os.environ.get("RENDITION_FAST_REVIEW")
    if fast_review:
        os.environ["RENDITION_FAST_REVIEW"] = "1"
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(file_bytes)
        temp_pdf_path = Path(tmp.name)
    try:
        result = run_rendition_pipeline(pdf_path=str(temp_pdf_path), manual_override=manual_override)
        return to_jsonable(result)
    finally:
        if fast_review:
            if previous_fast_review is None:
                os.environ.pop("RENDITION_FAST_REVIEW", None)
            else:
                os.environ["RENDITION_FAST_REVIEW"] = previous_fast_review
        try:
            temp_pdf_path.unlink(missing_ok=True)
        except Exception:
            pass


def render_pdf_pages(file_bytes: bytes) -> list[str]:
    import fitz  # PyMuPDF

    previews: list[str] = []
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    try:
        for page in doc:
            pix = page.get_pixmap(matrix=fitz.Matrix(1.35, 1.35), alpha=False)
            previews.append(_encode_bytes(pix.tobytes("png")))
    finally:
        doc.close()
    return previews


def build_batch_row(file_name: str, result: dict[str, Any]) -> dict[str, Any]:
    assessment = result.get("assessment_summary", {}) or {}
    metadata = result.get("metadata", {}) or {}
    review_flags = result.get("review_flags", {}) or {}
    agent_review = result.get("agent_review", {}) or {}
    form_flags = result.get("form_flags", {}) or {}
    schedule_e = result.get("schedule_e", {}) or {}
    schedule_values = result.get("schedule_values", {}) or {}
    attachments = result.get("attachments", {}) or {}
    value = (
        assessment.get("recommended_value")
        or assessment.get("recommended_market_value")
        or assessment.get("recommended_assessed_value")
        or assessment.get("extracted_value")
    )
    issues = assessment.get("issues", []) or []
    return {
        "File Name": file_name,
        "Tax Year": metadata.get("tax_year") or "-",
        "Owner Name": metadata.get("owner_name") or "-",
        "Account Number": metadata.get("account_number") or "-",
        "Recommended Value": value,
        "Extraction Provider": result.get("extraction_provider") or "-",
        "Valuation Path": assessment.get("recommended_path") or "-",
        "Confidence": assessment.get("confidence") or "-",
        "Status": get_status_label(result),
        "Value Source": assessment.get("value_source") or "-",
        "Signature Detected": bool(form_flags.get("signature_block_detected")),
        "SEE ATTACHED": bool(form_flags.get("see_attached")),
        "Schedule E Total": schedule_e.get("total"),
        "Schedule A GFE Total": schedule_values.get("good_faith_total"),
        "Historical Cost Total": schedule_values.get("historical_cost_total"),
        "Attachment Total": attachments.get("best_attachment_total"),
        "Agent Status": agent_review.get("status") or "-",
        "Agent Flags": " | ".join(str(x) for x in agent_review.get("review_flags", []) or []) or "-",
        "Candidates Found": len(result.get("value_candidates", []) or []),
        "Needs Manual Row Review": bool(review_flags.get("needs_manual_row_review")),
        "Needs Attachment Review": bool(review_flags.get("needs_attachment_review")),
        "Issues": " | ".join(issues) if issues else "-",
    }


def get_status_label(result: dict[str, Any]) -> str:
    assessment = result.get("assessment_summary", {}) or {}
    issues = assessment.get("issues", []) or []
    agent_review = result.get("agent_review", {}) or {}
    path = assessment.get("recommended_path")
    if path == "manual_review":
        return "Manual Review"
    if agent_review.get("status") == "fallback":
        return "Review Recommended"
    if issues:
        return "Review Recommended"
    return "Ready"


def _extract_costs(rows: list[dict[str, Any]]) -> dict[str, float]:
    costs: dict[str, float] = {}
    for row in rows:
        bucket = str(row.get("bucket") or "").strip()
        if not bucket or bucket in {"flat_value", "good_faith_value"}:
            continue
        costs[bucket] = _to_float(row.get("cost"))
    return costs


def _extract_good_faith_value(rows: list[dict[str, Any]]) -> float | None:
    for row in rows:
        if str(row.get("bucket") or "").strip() == "good_faith_value":
            return _to_float(row.get("cost", row.get("value")))
    return None


def _extract_flat_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flat_rows: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("bucket") or "").strip() != "flat_value":
            continue
        amount = _to_float(row.get("cost", row.get("value")))
        flat_rows.append(
            {
                "bucket": "flat_value",
                "display_year": str(row.get("display_year") or "Flat Value"),
                "year_acquired": None,
                "cost": amount,
                "factor": 1.0,
                "value": amount,
            }
        )
    return flat_rows


def _factor_sequence(table_key: str, custom_factors: list[float] | None = None) -> list[float]:
    if custom_factors:
        factors = [round(float(value), 6) for value in custom_factors]
        while len(factors) < 15:
            factors.append(factors[-1])
        return factors
    sequences = {
        "5_year": [0.75, 0.45, 0.20, 0.15, 0.10],
        "8_year": [0.75, 0.60, 0.45, 0.35, 0.25, 0.20, 0.10, 0.05],
        "9_year": [0.90, 0.80, 0.70, 0.60, 0.50, 0.40, 0.30, 0.20, 0.10],
        "12_year": [0.90, 0.80, 0.70, 0.65, 0.60, 0.55, 0.50, 0.45, 0.40, 0.35],
    }
    factors = list(sequences[table_key])
    while len(factors) < 15:
        factors.append(factors[-1])
    return factors


def _build_depreciation_rows(
    tax_year: int,
    table_key: str,
    costs: dict[str, float],
    *,
    good_faith_value: float | None = None,
    custom_factors: list[float] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if good_faith_value is not None:
        amount = round(float(good_faith_value or 0.0), 2)
        rows.append(
            {
                "bucket": "good_faith_value",
                "display_year": "Good Faith Estimate",
                "year_acquired": None,
                "cost": amount,
                "factor": 1.0,
                "value": amount,
            }
        )

    factors = _factor_sequence(table_key, custom_factors)
    base_year = int(tax_year) - 1
    for offset, factor in enumerate(factors):
        year = base_year - offset
        bucket = "prior" if offset == len(factors) - 1 else str(year)
        display_year = f"{year} & Prior" if bucket == "prior" else str(year)
        cost = round(float(costs.get(bucket, 0.0) or 0.0), 2)
        rows.append(
            {
                "bucket": bucket,
                "display_year": display_year,
                "year_acquired": None if bucket == "prior" else year,
                "cost": cost,
                "factor": round(float(factor), 2),
                "value": round(cost * factor, 2),
            }
        )
    return rows


def _infer_tax_year(rows: list[dict[str, Any]]) -> int | None:
    years: list[int] = []
    for row in rows:
        bucket = str(row.get("bucket") or "").strip()
        year_acquired = row.get("year_acquired")
        if bucket in {"flat_value", "good_faith_value", "prior"} or year_acquired in (None, ""):
            continue
        try:
            years.append(int(year_acquired))
        except (TypeError, ValueError):
            continue
    if not years:
        return None
    return max(years) + 1


def _calculate_breakdown(request: CalculateRequest) -> list[dict[str, Any]]:
    if not request.rows:
        raise HTTPException(status_code=400, detail="rows must contain at least one item.")
    for index, row in enumerate(request.rows):
        if not isinstance(row, dict):
            raise HTTPException(status_code=400, detail=f"rows[{index}] must be an object.")
    depreciation_table = DEPRECIATION_SCHEDULE_MAP.get(request.depreciation_schedule, request.depreciation_schedule)
    custom_factors = None
    if str(request.depreciation_schedule).startswith("cad_") and request.district_id:
        schedule_id = str(request.depreciation_schedule).removeprefix("cad_")
        custom_schedule = next(
            (item for item in fetch_cad_schedules_for_district(request.district_id) if str(item.get("id")) == schedule_id),
            None,
        )
        if custom_schedule:
            custom_factors = [float(value) for value in custom_schedule.get("factors", [])]
        else:
            depreciation_table = "9_year"
    if request.schedule_type == "B":
        amount = round(sum(_to_float(row.get("cost", row.get("value"))) for row in request.rows), 2)
        return [{
            "bucket": "flat_value",
            "display_year": "Schedule B - Inventory",
            "year_acquired": None,
            "cost": amount,
            "factor": 1.0,
            "value": amount,
        }]
    if request.schedule_type == "C":
        amount = round(sum(_to_float(row.get("cost", row.get("value"))) for row in request.rows), 2)
        return [{
            "bucket": "flat_value",
            "display_year": "Schedule C - Supplies",
            "year_acquired": None,
            "cost": amount,
            "factor": 1.0,
            "value": amount,
        }]
    tax_year = _infer_tax_year(request.rows)
    if tax_year is None:
        raise HTTPException(status_code=400, detail="Could not infer tax year from rows.")
    costs = _extract_costs(request.rows)
    good_faith_value = _extract_good_faith_value(request.rows)
    if request.schedule_type == "A":
        return _build_depreciation_rows(tax_year, depreciation_table, costs, good_faith_value=good_faith_value, custom_factors=custom_factors)
    if request.schedule_type == "D":
        return _extract_flat_rows(request.rows) + _build_depreciation_rows(tax_year, depreciation_table, costs, good_faith_value=good_faith_value, custom_factors=custom_factors)
    return _build_depreciation_rows(tax_year, depreciation_table, costs, custom_factors=custom_factors)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "appraisal-pilot-api"}


@app.get("/")
def root(page: str | None = None) -> FileResponse:
    if str(page or "").strip().lower() == "arb":
        return arb_root()
    index_path = FRONTEND_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="frontend/index.html was not found.")
    return FileResponse(index_path)


@app.get("/ARB")
def arb_root() -> FileResponse:
    page_path = arb_page_path(PROJECT_ROOT)
    if not page_path.exists():
        raise HTTPException(status_code=404, detail="frontend/arb.html was not found.")
    return FileResponse(page_path)


@app.get("/arb")
def arb_root_lowercase() -> FileResponse:
    return arb_root()


@app.get("/cad-onboarding")
def cad_onboarding_root() -> FileResponse:
    page_path = FRONTEND_DIR / "cad-onboarding.html"
    if not page_path.exists():
        raise HTTPException(status_code=404, detail="frontend/cad-onboarding.html was not found.")
    return FileResponse(page_path)


@app.get("/onboarding/cad")
def cad_onboarding_alias() -> FileResponse:
    return cad_onboarding_root()


@app.get("/api/bootstrap")
def bootstrap() -> dict[str, Any]:
    hydrate_analysis_env()
    return {
        "status": "ok",
        "service": "appraisal-pilot-api",
        "version": "v1",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "section_presets": SECTION_PRESETS,
        "table_metadata": TABLE_METADATA,
        "supabase_configured": bool(get_supabase_config()[1]),
        "service_role_configured": bool(get_supabase_service_role_key()),
        "provider_status": provider_status_snapshot(),
    }


@app.get("/api/info")
def info() -> dict[str, str]:
    return {
        "status": "ok",
        "service": "appraisal-pilot-api",
        "version": "v1",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/api/auth/login")
def auth_login(request: LoginRequest) -> dict[str, Any]:
    auth_result = sign_in_with_supabase(request.email.strip().lower(), request.password)
    access_token = auth_result.get("access_token")
    if not access_token:
        raise HTTPException(status_code=400, detail="Login did not return a session.")
    user = auth_result.get("user") or get_supabase_user(access_token)
    try:
        district = build_district_context(user, access_token)
    except DistrictServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not district:
        raise HTTPException(status_code=403, detail=UNLINKED_DISTRICT_MESSAGE)
    return {"access_token": access_token, "user": to_jsonable(user), "district": _district_to_dict(district)}


@app.post("/api/auth/restore")
def auth_restore(request: SessionRequest) -> dict[str, Any]:
    user = get_supabase_user(request.access_token)
    district = build_district_context(user, request.access_token)
    if not district:
        raise HTTPException(status_code=403, detail=UNLINKED_DISTRICT_MESSAGE)
    return {"access_token": request.access_token, "user": to_jsonable(user), "district": _district_to_dict(district)}


@app.post("/api/auth/signup")
def auth_signup(request: SignupRequest) -> dict[str, Any]:
    email = request.email.strip().lower()
    service_role_key = get_supabase_service_role_key()
    supabase_url, _anon_key = get_supabase_config()
    if not service_role_key:
        raise HTTPException(status_code=500, detail="SUPABASE_SERVICE_ROLE_KEY is required for account invitations.")
    if len(request.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")
    if request.password != request.confirm_password:
        raise HTTPException(status_code=400, detail="Passwords do not match.")
    try:
        invited_user = get_invited_district_user(
            supabase_url=supabase_url,
            service_role_key=service_role_key,
            email=email,
        )
    except DistrictServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not invited_user:
        raise HTTPException(
            status_code=403,
            detail="This email is not authorized for Rendition Pilot. Ask your CAD admin to add it first.",
        )
    signup_result = create_supabase_account(email, request.password)
    access_token = signup_result.get("access_token") or (signup_result.get("session") or {}).get("access_token")
    user = signup_result.get("user")
    if access_token and not user:
        user = get_supabase_user(access_token)
    if access_token:
        link_user_to_district(
            supabase_url=supabase_url,
            service_role_key=service_role_key,
            district_id=invited_user.district_id,
            email=email,
            user_id=str((user or {}).get("id") or "").strip() or None,
            role=invited_user.role,
        )
        district = build_district_context(user or {"email": email}, access_token)
        if not district:
            raise HTTPException(status_code=403, detail=UNLINKED_DISTRICT_MESSAGE)
        return {"access_token": access_token, "user": to_jsonable(user), "district": _district_to_dict(district)}
    return {"message": "Login created. Confirm the email if Supabase requires confirmation, then sign in."}


@app.post("/api/arb/auth/login")
def arb_auth_login(request: LoginRequest) -> dict[str, Any]:
    auth_result = sign_in_with_supabase(request.email.strip().lower(), request.password)
    access_token = auth_result.get("access_token")
    if not access_token:
        raise HTTPException(status_code=400, detail="Login did not return a session.")
    user = auth_result.get("user") or get_supabase_user(access_token)
    email = normalize_email(str(user.get("email") or request.email))
    user_id = str(user.get("id") or "").strip() or None
    access = get_app_access(email, "arb_pilot")
    if not access:
        district = build_district_context(user, access_token)
        if not district:
            raise HTTPException(status_code=403, detail=ARB_UNAUTHORIZED_MESSAGE)
        return {
            "access_token": access_token,
            "user": to_jsonable(user),
            "app_access": {
                "email": email,
                "app_name": "arb_pilot",
                "user_id": user_id,
                "role": district.role,
                "district_id": district.district_id,
            },
        }
    if user_id and access.get("user_id") != user_id:
        link_app_access_user(email, "arb_pilot", user_id)
        access = {**access, "user_id": user_id}
    return {"access_token": access_token, "user": to_jsonable(user), "app_access": to_jsonable(access)}


@app.post("/api/arb/auth/restore")
def arb_auth_restore(request: ARBAuthRequest) -> dict[str, Any]:
    return require_arb_access(request.access_token)


@app.post("/api/arb/auth/signup")
def arb_auth_signup(request: SignupRequest) -> dict[str, Any]:
    email = request.email.strip().lower()
    if len(request.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")
    if request.password != request.confirm_password:
        raise HTTPException(status_code=400, detail="Passwords do not match.")
    access = get_app_access(email, "arb_pilot")
    if not access:
        raise HTTPException(status_code=403, detail=ARB_UNAUTHORIZED_MESSAGE)
    signup_result = create_supabase_account_for_app(email, request.password, "arb_pilot")
    access_token = signup_result.get("access_token") or (signup_result.get("session") or {}).get("access_token")
    user = signup_result.get("user")
    if access_token and not user:
        user = get_supabase_user(access_token)
    user_id = str((user or {}).get("id") or "").strip() or None
    if user_id:
        link_app_access_user(email, "arb_pilot", user_id)
        access = {**access, "user_id": user_id}
    if access_token:
        return {"access_token": access_token, "user": to_jsonable(user), "app_access": to_jsonable(access)}
    return {"message": "Login created. Confirm the email if Supabase requires confirmation, then sign in."}


@app.post("/api/auth/district-setup")
def auth_district_setup(request: DistrictSetupRequest) -> dict[str, Any]:
    service_role_key = get_supabase_service_role_key()
    supabase_url, _anon_key = get_supabase_config()
    if not service_role_key:
        raise HTTPException(status_code=400, detail="SUPABASE_SERVICE_ROLE_KEY is required for CAD onboarding.")
    verify_supabase_district_setup(supabase_url=supabase_url, service_role_key=service_role_key)
    if len(request.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")
    if request.password != request.confirm_password:
        raise HTTPException(status_code=400, detail="Passwords do not match.")
    district_slug = slugify_district_slug(request.district_slug) or slugify_district_name(request.district_name)
    district_domain = infer_domain_from_email(request.admin_email)
    district = create_or_update_district(
        supabase_url=supabase_url,
        service_role_key=service_role_key,
        name=request.district_name,
        slug=district_slug,
        domain=district_domain,
    )
    signup_result = create_supabase_account(request.admin_email.strip().lower(), request.password)
    access_token = signup_result.get("access_token") or (signup_result.get("session") or {}).get("access_token")
    user = signup_result.get("user")
    if access_token and not user:
        user = get_supabase_user(access_token)
    user_id = str((user or {}).get("id") or "").strip() or None
    link_user_to_district(
        supabase_url=supabase_url,
        service_role_key=service_role_key,
        district_id=district.district_id,
        email=request.admin_email.strip().lower(),
        user_id=user_id,
        role="admin",
    )
    if not access_token:
        return {"message": "District created and user linked. Confirm the email if required, then sign in."}
    linked_district = build_district_context(user or {"email": request.admin_email}, access_token)
    if not linked_district:
        raise HTTPException(status_code=403, detail=UNLINKED_DISTRICT_MESSAGE)
    return {"access_token": access_token, "user": to_jsonable(user), "district": _district_to_dict(linked_district)}


@app.post("/api/cad-onboarding")
def cad_onboarding(request: CadOnboardingRequest) -> dict[str, Any]:
    validate_cad_onboarding_payload(request)
    service_role_key = get_supabase_service_role_key()
    supabase_url, _anon_key = get_supabase_config()
    if not service_role_key:
        raise HTTPException(status_code=500, detail="SUPABASE_SERVICE_ROLE_KEY is required for CAD onboarding.")
    verify_supabase_district_setup(supabase_url=supabase_url, service_role_key=service_role_key)

    admin_email = normalize_email(request.admin.email)
    district_slug = slugify_district_slug(request.district_slug) or slugify_district_name(request.district.cad_name)
    district_domain = infer_domain_from_email(admin_email)
    district = create_or_update_district(
        supabase_url=supabase_url,
        service_role_key=service_role_key,
        name=request.district.display_name.strip() or request.district.cad_name.strip(),
        slug=district_slug,
        domain=district_domain,
    )

    cad_profile_payload = {
        "district_id": district.district_id,
        "cad_name": request.district.cad_name.strip(),
        "display_name": request.district.display_name.strip() or request.district.cad_name.strip(),
        "email": normalize_email(request.district.email),
        "phone": request.district.phone.strip(),
        "address": request.district.address.strip(),
        "website": request.district.website.strip() or None,
        "onboarding_completed": True,
    }
    existing_profile = fetch_cad_profile_by_district_id(district.district_id)
    if existing_profile:
        cad_profile = cad_data_request(
            "PATCH",
            "cad_districts",
            params={"id": f"eq.{existing_profile['id']}"},
            payload=cad_profile_payload,
        )
        cad_profile = cad_profile[0] if isinstance(cad_profile, list) and cad_profile else {**existing_profile, **cad_profile_payload}
    else:
        created_profile = cad_data_request("POST", "cad_districts", payload=cad_profile_payload)
        cad_profile = created_profile[0] if isinstance(created_profile, list) else created_profile

    access_token = None
    user: dict[str, Any] | None = None
    if request.admin_password:
        try:
            signup_result = create_supabase_account(admin_email, request.admin_password)
            access_token = signup_result.get("access_token") or (signup_result.get("session") or {}).get("access_token")
            user = signup_result.get("user")
            if access_token and not user:
                user = get_supabase_user(access_token)
        except HTTPException:
            user = None
            access_token = None
    user_id = str((user or {}).get("id") or "").strip() or None

    all_users: list[tuple[CadUserRequest, bool]] = [(request.admin, True)]
    seen_emails = {admin_email}
    for item in request.users:
        email = normalize_email(item.email)
        if not email or email in seen_emails:
            continue
        seen_emails.add(email)
        all_users.append((item, False))

    linked_users: list[dict[str, Any]] = []
    for item, is_admin in all_users:
        email = normalize_email(item.email)
        if "@" not in email:
            raise HTTPException(status_code=400, detail=f"Enter a valid email address for {item.email or 'CAD user'}.")
        link_user_to_district(
            supabase_url=supabase_url,
            service_role_key=service_role_key,
            district_id=district.district_id,
            email=email,
            user_id=user_id if is_admin else None,
            role="admin" if is_admin else "member",
        )
        seed_arb_access_for_district_user(email, user_id if is_admin else None, role="admin" if is_admin else "member")

    district_users = list_district_users(
        supabase_url=supabase_url,
        service_role_key=service_role_key,
        district_id=district.district_id,
    )
    district_user_by_email = {normalize_email(row.get("email", "")): row for row in district_users}
    for item, is_admin in all_users:
        email = normalize_email(item.email)
        district_user = district_user_by_email.get(email) or {}
        cad_user_payload = {
            "district_id": cad_profile["id"],
            "district_user_id": district_user.get("id"),
            "first_name": item.first_name.strip(),
            "last_name": item.last_name.strip(),
            "email": email,
            "role_title": item.role_title.strip(),
            "is_admin": is_admin,
        }
        existing = cad_data_request(
            "GET",
            "cad_users",
            params={"select": "*", "district_id": f"eq.{cad_profile['id']}", "email": f"eq.{email}", "limit": "1"},
        )
        if existing:
            saved = cad_data_request("PATCH", "cad_users", params={"id": f"eq.{existing[0]['id']}"}, payload=cad_user_payload)
            linked_users.append(saved[0] if isinstance(saved, list) and saved else {**existing[0], **cad_user_payload})
        else:
            saved = cad_data_request("POST", "cad_users", payload=cad_user_payload)
            linked_users.append(saved[0] if isinstance(saved, list) else saved)

    existing_schedules = fetch_cad_schedules_for_district(district.district_id)
    if not existing_schedules:
        for schedule in request.schedules:
            create_cad_schedule(cad_profile["id"], schedule)

    schedules = fetch_cad_schedules_for_district(district.district_id)
    response = {
        "district": _district_to_dict(district),
        "cad_profile": to_jsonable(cad_profile),
        "users": to_jsonable(linked_users),
        "schedules": to_jsonable(schedules),
        "message": "CAD onboarding completed.",
    }
    if access_token:
        linked_district = build_district_context(user or {"email": admin_email}, access_token)
        if linked_district:
            response.update({"access_token": access_token, "user": to_jsonable(user), "district": _district_to_dict(linked_district)})
    return response


@app.get("/api/cad-profile/{district_id}")
def cad_profile(district_id: str) -> dict[str, Any]:
    profile = fetch_cad_profile_by_district_id(district_id)
    if not profile:
        return {"cad_profile": None, "users": [], "schedules": []}
    users = cad_data_request(
        "GET",
        "cad_users",
        params={"select": "*", "district_id": f"eq.{profile['id']}", "order": "created_at.asc"},
    )
    return {
        "cad_profile": to_jsonable(profile),
        "users": to_jsonable(users if isinstance(users, list) else []),
        "schedules": to_jsonable(fetch_cad_schedules_for_district(district_id)),
    }


@app.get("/api/cad-schedules/{district_id}")
def cad_schedules(district_id: str) -> dict[str, Any]:
    return {"schedules": to_jsonable(fetch_cad_schedules_for_district(district_id))}


@app.post("/api/cad-schedules/{district_id}")
def cad_schedule_create(district_id: str, request: CadScheduleSaveRequest) -> dict[str, Any]:
    district = require_district_admin(request.access_token)
    if district.district_id != district_id:
        raise HTTPException(status_code=403, detail="Cannot manage schedules for another district.")
    profile = fetch_cad_profile_by_district_id(district_id)
    if not profile:
        raise HTTPException(status_code=404, detail="CAD onboarding profile was not found for this district.")
    schedule = create_cad_schedule(profile["id"], request.schedule)
    return {"schedule": to_jsonable(schedule), "schedules": to_jsonable(fetch_cad_schedules_for_district(district_id))}


@app.put("/api/cad-schedules/{schedule_id}")
def cad_schedule_update(schedule_id: str, schedule: CadScheduleRequest) -> dict[str, Any]:
    existing = fetch_cad_schedule(schedule_id)
    if not existing:
        raise HTTPException(status_code=404, detail="CAD schedule was not found.")
    updated = replace_cad_schedule(schedule_id, schedule)
    return {"schedule": to_jsonable(updated)}


@app.delete("/api/cad-schedules/{schedule_id}")
def cad_schedule_delete(schedule_id: str) -> dict[str, Any]:
    existing = fetch_cad_schedule(schedule_id)
    if not existing:
        raise HTTPException(status_code=404, detail="CAD schedule was not found.")
    updated = cad_data_request(
        "PATCH",
        "cad_depreciation_schedules",
        params={"id": f"eq.{schedule_id}"},
        payload={"is_active": False},
    )
    return {"schedule": to_jsonable(updated[0] if isinstance(updated, list) and updated else {**existing, "is_active": False})}


@app.post("/api/district/users")
def district_user_list(request: SessionRequest) -> dict[str, Any]:
    service_role_key = get_supabase_service_role_key()
    supabase_url, _anon_key = get_supabase_config()
    if not service_role_key:
        raise HTTPException(status_code=500, detail="SUPABASE_SERVICE_ROLE_KEY is required for district user management.")
    district = require_district_admin(request.access_token)
    try:
        users = list_district_users(
            supabase_url=supabase_url,
            service_role_key=service_role_key,
            district_id=district.district_id,
        )
    except DistrictServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"district": _district_to_dict(district), "users": to_jsonable(users)}


@app.post("/api/district/users/invite")
def district_user_invite(request: DistrictInviteRequest) -> dict[str, Any]:
    service_role_key = get_supabase_service_role_key()
    supabase_url, _anon_key = get_supabase_config()
    if not service_role_key:
        raise HTTPException(status_code=500, detail="SUPABASE_SERVICE_ROLE_KEY is required for district user management.")
    district = require_district_admin(request.access_token)
    email = normalize_email(request.email)
    if "@" not in email:
        raise HTTPException(status_code=400, detail="Enter a valid email address.")
    try:
        link_user_to_district(
            supabase_url=supabase_url,
            service_role_key=service_role_key,
            district_id=district.district_id,
            email=email,
            role=request.role,
        )
        users = list_district_users(
            supabase_url=supabase_url,
            service_role_key=service_role_key,
            district_id=district.district_id,
        )
    except DistrictServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"district": _district_to_dict(district), "users": to_jsonable(users)}


@app.post("/api/pdf/render")
def pdf_render(request: PdfRequest) -> dict[str, Any]:
    file_bytes = _decode_pdf(request.file_base64)
    return {"pages": render_pdf_pages(file_bytes)}


@app.post("/api/arb/review")
def arb_review(request: ARBReviewRunRequest) -> dict[str, Any]:
    hydrate_analysis_env()
    require_arb_access(request.access_token)
    try:
        cad_bytes = decode_upload_base64(request.cad_packet.file_base64)
        taxpayer_bytes = decode_upload_base64(request.taxpayer_packet.file_base64)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid evidence upload payload: {exc}") from exc

    cad_packet = parse_evidence_packet(
        request.cad_packet.file_name,
        cad_bytes,
        "CAD Evidence Packet",
    )
    taxpayer_packet = parse_evidence_packet(
        request.taxpayer_packet.file_name,
        taxpayer_bytes,
        "Agent / Taxpayer Evidence Packet",
    )
    resolved_case_info = infer_case_info(cad_packet, taxpayer_packet, request.case_info)
    summary = analyze_arb_evidence(cad_packet, taxpayer_packet, resolved_case_info)
    return {
        "case_info": to_jsonable(resolved_case_info.model_dump()),
        "cad_packet": to_jsonable(cad_packet.model_dump()),
        "taxpayer_packet": to_jsonable(taxpayer_packet.model_dump()),
        "summary": to_jsonable(summary.model_dump()),
        "summary_text": build_summary_text(summary, resolved_case_info),
        "provider_status": provider_status_snapshot(),
    }


@app.post("/api/arb/evidence-packet")
def arb_evidence_packet(request: ARBPacketUpdateRequest) -> dict[str, Any]:
    require_arb_access(request.access_token)
    try:
        cad_bytes = decode_upload_base64(request.cad_packet.file_base64)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid CAD evidence packet payload: {exc}") from exc
    try:
        file_name, packet_bytes = build_updated_evidence_packet(
            cad_pdf_bytes=cad_bytes,
            case_info=request.case_info,
            selected_sections=request.selected_sections,
            rebuttal_argument=request.rebuttal_argument,
            hearing_prep=request.hearing_prep,
            copy_ready_rebuttal=request.copy_ready_rebuttal,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "file_name": file_name,
        "pdf_base64": _encode_bytes(packet_bytes),
    }


@app.post("/api/review/run")
def review_run(request: ReviewRunRequest) -> dict[str, Any]:
    from app.cli import build_cli_summary

    file_bytes = _decode_pdf(request.file_base64)
    manual_override = to_jsonable(request.manual_override.model_dump()) if request.manual_override else None
    result = run_pipeline_from_upload(
        request.file_name,
        file_bytes,
        manual_override=manual_override,
        fast_review=bool(request.fast_review),
    )
    payload = {
        "file_name": request.file_name,
        "result": result,
        "summary": build_cli_summary(result=result, source_path=request.file_name),
    }
    if request.include_pdf_pages:
        payload["pdf_pages"] = render_pdf_pages(file_bytes)
    return payload


@app.post("/api/batch/run")
def batch_run_disabled() -> dict[str, Any]:
    raise HTTPException(status_code=410, detail="Batch review is disabled. Process one rendition PDF at a time.")


@app.post("/api/calculate")
def calculate(request: CalculateRequest) -> dict[str, Any]:
    breakdown = _calculate_breakdown(request)
    total_value = round(sum(_to_float(row.get("value")) for row in breakdown), 2)
    return {"total_value": total_value, "breakdown": breakdown}


@app.post("/api/review/lock")
def review_lock(request: LockReviewRequest) -> dict[str, Any]:
    decision_code = str(request.decision or "accepted").strip().lower()
    final_value = request.final_value
    if final_value is None and is_zero_value_decision(decision_code):
        final_value = 0.0
    if final_value is None:
        raise HTTPException(status_code=400, detail="Enter a valid final value before locking.")
    if not request.appraiser_initials.strip():
        raise HTTPException(status_code=400, detail="Enter appraiser initials before locking.")
    if not request.account_number.strip():
        raise HTTPException(status_code=400, detail="Enter the appraisal district account / P# before locking.")
    record = build_final_review_record(
        file_name=request.file_name,
        result=request.result,
        final_value=final_value,
        final_source=request.final_source,
        appraiser_notes=request.appraiser_notes,
        appraiser_initials=request.appraiser_initials.strip().upper(),
        account_number=request.account_number.strip().upper(),
        decision=decision_code,
        district_context=request.district_context,
    )
    record["saved_calculators"] = request.saved_calculators
    record["calculated_total_value"] = calculate_combined_total(request.saved_calculators)
    return {
        "final_record": to_jsonable(record),
        "decision_label": get_decision_label(record.get("decision")),
    }


@app.post("/api/review/save")
def review_save(request: SaveReviewRequest) -> dict[str, Any]:
    file_bytes = _decode_pdf(request.file_base64)
    district_slug = str((request.district_context or {}).get("district_slug") or "").strip() or None
    stamped_pdf = stamp_reviewed_pdf(
        file_name=request.file_name,
        file_bytes=file_bytes,
        final_record=request.final_record,
        district_slug=district_slug,
    )
    paths = save_review_outputs(
        file_name=request.file_name,
        result=request.result,
        final_record=request.final_record,
        district_context=request.district_context,
    )
    return {
        "stamped_pdf_name": stamped_pdf.name,
        "stamped_pdf_base64": _encode_bytes(stamped_pdf.read_bytes()),
        "stamped_pdf_path": str(stamped_pdf),
        "saved_outputs": [str(path) for path in {**paths, "stamped_pdf": stamped_pdf}.values()],
    }


@app.get("/api/review-queue")
def review_queue_disabled() -> dict[str, Any]:
    raise HTTPException(status_code=410, detail="Review queue is disabled.")
