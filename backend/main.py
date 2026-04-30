from __future__ import annotations

import base64
import csv
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

from app.district_service import (
    DistrictContext,
    DistrictServiceError,
    create_or_update_district,
    find_district_by_domain,
    infer_domain_from_email,
    link_user_to_district,
    normalize_email,
    resolve_district_for_user,
    slugify_district_name,
    slugify_district_slug,
    verify_supabase_district_setup,
)
from app.review_workflow import (
    append_queue_row,
    backfill_legacy_outputs,
    build_final_review_record,
    ensure_output_dirs,
    get_decision_label,
    get_recommended_value,
    save_review_outputs,
    stamp_reviewed_pdf,
)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")
load_dotenv(PROJECT_ROOT / "app" / ".env")
DEFAULT_SUPABASE_URL = "https://pzawjgckzcgnfsfuylqy.supabase.co"
DEFAULT_SUPABASE_ANON_KEY = "sb_publishable_q6lNn59Y-kz8lG0cYfJkYw_lL7xElsA"
UNLINKED_DISTRICT_MESSAGE = "Your account is not currently linked to an appraisal district. Please contact an administrator."
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


app = FastAPI(title="AppraisalPilot API")
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
    schedule_type: Literal["A", "B", "C", "D", "E"]
    rows: list[dict[str, Any]]
    depreciation_schedule: Literal["5", "8", "9", "12"]


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


class BatchItem(BaseModel):
    file_name: str
    file_base64: str


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


class BatchRunRequest(BaseModel):
    files: list[BatchItem]
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
    }


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


def run_pipeline_from_upload(file_name: str, file_bytes: bytes, manual_override: dict[str, Any] | None = None) -> dict[str, Any]:
    from app.pipeline import run_rendition_pipeline

    hydrate_analysis_env()
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(file_bytes)
        temp_pdf_path = Path(tmp.name)
    try:
        result = run_rendition_pipeline(pdf_path=str(temp_pdf_path), manual_override=manual_override)
        return to_jsonable(result)
    finally:
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


def _factor_sequence(table_key: str) -> list[float]:
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

    factors = _factor_sequence(table_key)
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
    depreciation_table = DEPRECIATION_SCHEDULE_MAP[request.depreciation_schedule]
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
        return _build_depreciation_rows(tax_year, depreciation_table, costs, good_faith_value=good_faith_value)
    if request.schedule_type == "D":
        return _extract_flat_rows(request.rows) + _build_depreciation_rows(tax_year, depreciation_table, costs, good_faith_value=good_faith_value)
    return _build_depreciation_rows(tax_year, depreciation_table, costs)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "appraisal-pilot-api"}


@app.get("/")
def root() -> FileResponse:
    index_path = FRONTEND_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="frontend/index.html was not found.")
    return FileResponse(index_path)


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
    backfill_legacy_outputs(district.district_slug)
    return {"access_token": access_token, "user": to_jsonable(user), "district": _district_to_dict(district)}


@app.post("/api/auth/restore")
def auth_restore(request: SessionRequest) -> dict[str, Any]:
    user = get_supabase_user(request.access_token)
    district = build_district_context(user, request.access_token)
    if not district:
        raise HTTPException(status_code=403, detail=UNLINKED_DISTRICT_MESSAGE)
    backfill_legacy_outputs(district.district_slug)
    return {"access_token": request.access_token, "user": to_jsonable(user), "district": _district_to_dict(district)}


@app.post("/api/auth/signup")
def auth_signup(request: SignupRequest) -> dict[str, Any]:
    email = request.email.strip().lower()
    if len(request.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")
    if request.password != request.confirm_password:
        raise HTTPException(status_code=400, detail="Passwords do not match.")
    signup_result = create_supabase_account(email, request.password)
    access_token = signup_result.get("access_token") or (signup_result.get("session") or {}).get("access_token")
    user = signup_result.get("user")
    if access_token and not user:
        user = get_supabase_user(access_token)
    if access_token:
        supabase_url, _anon_key = get_supabase_config()
        service_role_key = get_supabase_service_role_key()
        email_domain = infer_domain_from_email(email)
        if service_role_key and email_domain:
            domain_district = find_district_by_domain(
                supabase_url=supabase_url,
                service_role_key=service_role_key,
                domain=email_domain,
            )
            if domain_district:
                link_user_to_district(
                    supabase_url=supabase_url,
                    service_role_key=service_role_key,
                    district_id=domain_district.district_id,
                    email=email,
                    user_id=str((user or {}).get("id") or "").strip() or None,
                )
        district = build_district_context(user or {"email": email}, access_token)
        if not district:
            raise HTTPException(status_code=403, detail=UNLINKED_DISTRICT_MESSAGE)
        backfill_legacy_outputs(district.district_slug)
        return {"access_token": access_token, "user": to_jsonable(user), "district": _district_to_dict(district)}
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
    )
    if not access_token:
        return {"message": "District created and user linked. Confirm the email if required, then sign in."}
    linked_district = build_district_context(user or {"email": request.admin_email}, access_token)
    if not linked_district:
        raise HTTPException(status_code=403, detail=UNLINKED_DISTRICT_MESSAGE)
    backfill_legacy_outputs(linked_district.district_slug)
    return {"access_token": access_token, "user": to_jsonable(user), "district": _district_to_dict(linked_district)}


@app.post("/api/pdf/render")
def pdf_render(request: PdfRequest) -> dict[str, Any]:
    file_bytes = _decode_pdf(request.file_base64)
    return {"pages": render_pdf_pages(file_bytes)}


@app.post("/api/review/run")
def review_run(request: ReviewRunRequest) -> dict[str, Any]:
    from app.cli import build_cli_summary

    file_bytes = _decode_pdf(request.file_base64)
    manual_override = to_jsonable(request.manual_override.model_dump()) if request.manual_override else None
    result = run_pipeline_from_upload(request.file_name, file_bytes, manual_override=manual_override)
    return {
        "file_name": request.file_name,
        "result": result,
        "summary": build_cli_summary(result=result, source_path=request.file_name),
        "pdf_pages": render_pdf_pages(file_bytes),
    }


@app.post("/api/batch/run")
def batch_run(request: BatchRunRequest) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    results_payload: dict[str, Any] = {}
    district_context = request.district_context
    for item in request.files:
        file_bytes = _decode_pdf(item.file_base64)
        result = run_pipeline_from_upload(item.file_name, file_bytes, manual_override=None)
        rows.append(build_batch_row(item.file_name, result))
        results_payload[item.file_name] = result
        save_review_outputs(item.file_name, result, district_context=district_context)
        append_queue_row(item.file_name, result, status=get_status_label(result), district_context=district_context)
    return {"rows": rows, "results": results_payload}


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
    append_queue_row(
        file_name=request.file_name,
        result={**request.result, "final_review": request.final_record},
        status="Locked",
        district_context=request.district_context,
    )
    return {
        "stamped_pdf_name": stamped_pdf.name,
        "stamped_pdf_base64": _encode_bytes(stamped_pdf.read_bytes()),
        "stamped_pdf_path": str(stamped_pdf),
        "saved_outputs": [str(path) for path in {**paths, "stamped_pdf": stamped_pdf}.values()],
    }


@app.get("/api/review-queue")
def review_queue(district_slug: str | None = None) -> dict[str, Any]:
    backfill_legacy_outputs(district_slug)
    paths = ensure_output_dirs(district_slug)
    queue_rows: list[dict[str, Any]] = []
    if paths["queue_csv"].exists():
        with paths["queue_csv"].open("r", encoding="utf-8", newline="") as handle:
            queue_rows = list(csv.DictReader(handle))
    completed_rows: list[dict[str, Any]] = []
    for path in sorted(paths["completed"].glob("*_final.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        completed_rows.append(
            {
                "file_name": data.get("file_name", path.name),
                "final_value": data.get("final_value"),
                "final_source": data.get("final_source", "-"),
                "locked_at": data.get("locked_at", "-"),
                "notes": data.get("appraiser_notes", ""),
            }
        )
    return {
        "root": str(paths["root"]),
        "queue_rows": queue_rows,
        "completed_rows": completed_rows,
    }
