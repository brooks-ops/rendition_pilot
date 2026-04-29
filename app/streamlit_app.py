from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from io import BytesIO
from datetime import datetime
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF
import pandas as pd
import requests
import streamlit as st
from PIL import Image

APP_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = APP_DIR.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.cli import build_cli_summary
from app.depreciation import DepreciationEngine
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
from app.pipeline import run_rendition_pipeline
from app.rendition_calculator import (
    SECTION_PRESETS,
    TABLE_METADATA,
    build_calculator_rows,
    build_saved_calculator,
    calculate_combined_total,
    calculate_section_total,
    generate_calculator_name,
    load_depreciation_tables,
    resolve_tax_year,
)
from app.review_workflow import (
    OUTPUT_DIR,
    append_queue_row,
    backfill_legacy_outputs,
    build_final_review_record,
    ensure_output_dirs,
    get_decision_label,
    get_output_paths,
    get_recommended_value,
    save_review_outputs,
    stamp_reviewed_pdf,
)

DEFAULT_SUPABASE_URL = "https://pzawjgckzcgnfsfuylqy.supabase.co"
DEFAULT_SUPABASE_ANON_KEY = "sb_publishable_q6lNn59Y-kz8lG0cYfJkYw_lL7xElsA"
UNLINKED_DISTRICT_MESSAGE = "Your account is not currently linked to an appraisal district. Please contact an administrator."
AUTH_SESSION_KEYS = {
    "authenticated_user",
    "authenticated_user_id",
    "supabase_access_token",
    "district_id",
    "district_slug",
    "district_name",
    "district_domain",
}

st.set_page_config(
    page_title="AppraisalPilot",
    page_icon="📄",
    layout="wide",
)

st.markdown(
    """
    <style>
        .stApp {
            background-color: #0B1F3A;
            color: #FFFFFF;
        }

        html, body, [class*="css"] {
            color: #E6EDF5 !important;
        }

        .block-container {
            padding-top: 2rem;
            padding-bottom: 2rem;
            max-width: 1500px;
        }

        h1, h2, h3 {
            color: #FFD700 !important;
        }

        label, .stSelectbox label, .stTextInput label, .stNumberInput label, .stTextArea label {
            color: #E6EDF5 !important;
            font-weight: 600;
        }

        input, textarea, select {
            background-color: #112B4A !important;
            color: #FFFFFF !important;
            border: 1px solid rgba(255, 215, 0, 0.25) !important;
        }

        ::placeholder {
            color: #AAB8CC !important;
        }

        .stButton > button {
            background-color: #FFD700;
            color: #0B1F3A;
            font-weight: 700;
            border: none;
            border-radius: 10px;
            min-height: 44px;
        }

        .stButton > button:hover {
            background-color: #e6c200;
            color: #0B1F3A;
        }

        .stTabs [data-baseweb="tab-list"] {
            gap: 18px;
        }

        .stTabs [data-baseweb="tab"] {
            color: #D7DFEA !important;
            font-weight: 600;
        }

        .stTabs [aria-selected="true"] {
            color: #FFD700 !important;
        }

        div[data-testid="stFileUploader"] {
            background: #102948;
            border-radius: 14px;
            padding: 12px;
        }

        .stDataFrame {
            border: 1px solid rgba(255, 215, 0, 0.15);
            border-radius: 12px;
            overflow: hidden;
        }

        .ap-title {
            font-size: 2.2rem;
            font-weight: 800;
            color: #FFD700;
            margin-bottom: 0.25rem;
        }

        .ap-subtitle {
            color: #D7DFEA;
            margin-bottom: 1.25rem;
        }

        .ap-card {
            background: #102948;
            border: 1px solid rgba(255, 215, 0, 0.20);
            border-radius: 16px;
            padding: 18px;
            margin-bottom: 16px;
        }

        .ap-card-tight {
            padding: 14px 16px;
        }

        .ap-toolbar-row {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            flex-wrap: wrap;
            margin-bottom: 10px;
        }

        .ap-toolbar-meta {
            color: #C7D2E3;
            font-size: 0.9rem;
        }

        .ap-section-head {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            margin-bottom: 10px;
        }

        .ap-section-head h3 {
            margin: 0;
        }

        .ap-panel-note {
            color: #D7DFEA;
            font-size: 0.92rem;
            margin-bottom: 10px;
        }

        .ap-muted {
            color: #C7D2E3 !important;
            font-size: 0.95rem;
        }

        .ap-kv-row {
            display: flex;
            justify-content: space-between;
            gap: 16px;
            padding: 8px 0;
            border-bottom: 1px solid rgba(255,255,255,0.08);
        }

        .ap-kv-row:last-child {
            border-bottom: none;
        }

        .ap-kv-label {
            color: #C7D2E3;
            font-weight: 600;
        }

        .ap-kv-value {
            color: #FFFFFF;
            text-align: right;
            font-weight: 500;
        }

        .ap-decision-card {
            background: #102948;
            border: 2px solid rgba(255, 215, 0, 0.35);
            border-radius: 18px;
            padding: 22px;
            margin-bottom: 18px;
        }

        .ap-decision-label {
            color: #C7D2E3;
            font-size: 0.85rem;
            text-transform: uppercase;
            letter-spacing: 0.08em;
        }

        .ap-decision-value {
            color: #FFFFFF;
            font-size: 2.6rem;
            font-weight: 800;
            margin-top: 6px;
            margin-bottom: 16px;
        }

        .ap-mini-card {
            background: #0f2a44;
            border-radius: 14px;
            padding: 16px;
            min-height: 108px;
        }

        .ap-mini-label {
            font-size: 0.9rem;
            color: #D7DFEA;
            margin-bottom: 8px;
        }

        .ap-mini-value {
            font-size: 1.7rem;
            font-weight: 800;
            color: #FFFFFF;
            line-height: 1.2;
            word-break: break-word;
        }

        .ap-reason-box {
            background: #112f4e;
            border-left: 5px solid #FFD700;
            padding: 16px;
            border-radius: 10px;
            color: #FFFFFF;
            margin-top: 10px;
            margin-bottom: 14px;
        }

        .ap-analysis-grid {
            display: grid;
            grid-template-columns: repeat(4, minmax(0, 1fr));
            gap: 12px;
            margin-top: 12px;
        }

        .ap-analysis-grid .ap-mini-card {
            min-height: 88px;
            padding: 14px;
        }

        .ap-workbench-pane {
            max-height: calc(100vh - 150px);
        }

        .ap-workbench-pane h4 {
            margin: 0;
            color: #FFD700;
            font-size: 1.05rem;
            font-weight: 700;
        }

        .ap-calc-label {
            color: #D7DFEA;
            font-size: 0.72rem;
            font-weight: 700;
            letter-spacing: 0.04em;
            text-transform: uppercase;
            margin-bottom: 4px;
        }

        .ap-calc-subtle {
            color: #B9C6D9;
            font-size: 0.82rem;
            line-height: 1.35;
        }

        .ap-calc-table-head {
            display: grid;
            grid-template-columns: 0.7fr 1.35fr 0.75fr 1fr;
            gap: 10px;
            align-items: center;
            padding: 0 4px 6px 4px;
            margin-top: 8px;
            border-bottom: 1px solid rgba(255,255,255,0.08);
        }

        .ap-calc-table-head div {
            color: #D7DFEA;
            font-size: 0.76rem;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.04em;
        }

        .ap-calc-year {
            color: #FFFFFF;
            font-size: 0.9rem;
            font-weight: 700;
            padding-top: 6px;
        }

        .ap-calc-factor {
            display: inline-block;
            padding: 3px 8px;
            border-radius: 999px;
            background: rgba(255, 215, 0, 0.12);
            border: 1px solid rgba(255, 215, 0, 0.25);
            color: #FFD700;
            font-size: 0.8rem;
            font-weight: 700;
            line-height: 1;
        }

        .ap-calc-value {
            color: #FFFFFF;
            font-size: 0.9rem;
            font-weight: 700;
            text-align: right;
            padding-top: 6px;
            white-space: nowrap;
        }

        .ap-calc-footer {
            margin-top: 10px;
            padding-top: 10px;
            border-top: 1px solid rgba(255,255,255,0.08);
        }

        .ap-calc-total {
            color: #FFFFFF;
            font-size: 1.25rem;
            font-weight: 800;
            text-align: right;
            white-space: nowrap;
        }

        .ap-calc-total-label {
            color: #D7DFEA;
            font-size: 0.82rem;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.04em;
        }

        .ap-saved-work-card {
            background: #0f2a44;
            border: 1px solid rgba(255, 215, 0, 0.14);
            border-radius: 12px;
            padding: 12px;
            margin-top: 10px;
        }

        .ap-saved-work-title {
            color: #FFD700;
            font-size: 0.95rem;
            font-weight: 700;
            margin-bottom: 6px;
        }

        div[data-testid="stNumberInput"] input,
        div[data-testid="stTextInput"] input,
        div[data-testid="stSelectbox"] div[data-baseweb="select"] {
            min-height: 34px !important;
        }

        div[data-testid="stNumberInput"] button {
            min-height: 28px !important;
            min-width: 28px !important;
        }
        div[data-testid="stMetricValue"],
        div[data-testid="stMetricValue"] div,
        div[data-testid="stMetricLabel"],
        div[data-testid="stMetricLabel"] div {
            color: #FFFFFF !important;
        }

        .streamlit-expanderHeader {
            color: #FFFFFF !important;
            font-weight: 700;
        }
    </style>
    """,
    unsafe_allow_html=True,
)


def get_secret(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value:
        return value

    try:
        return str(st.secrets.get(name, default) or default)
    except Exception:
        return default


def hydrate_analysis_env_from_secrets() -> None:
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
        "GOOGLE_APPLICATION_CREDENTIALS",
    ]

    for name in secret_names:
        if os.getenv(name):
            continue
        value = get_secret(name, "")
        if value:
            os.environ[name] = value


def get_supabase_config() -> tuple[str, str]:
    return (
        get_secret("SUPABASE_URL", DEFAULT_SUPABASE_URL).rstrip("/"),
        get_secret("SUPABASE_ANON_KEY", DEFAULT_SUPABASE_ANON_KEY),
    )


def get_supabase_service_role_key() -> str:
    return get_secret("SUPABASE_SERVICE_ROLE_KEY", "")


def clear_non_auth_session_state() -> None:
    for key in list(st.session_state.keys()):
        if key not in AUTH_SESSION_KEYS:
            st.session_state.pop(key, None)


def get_session_district_context() -> dict[str, str | None] | None:
    district_id = str(st.session_state.get("district_id") or "").strip()
    district_slug = str(st.session_state.get("district_slug") or "").strip()
    district_name = str(st.session_state.get("district_name") or "").strip()
    if not district_id or not district_slug or not district_name:
        return None
    return {
        "district_id": district_id,
        "district_slug": district_slug,
        "district_name": district_name,
        "district_domain": str(st.session_state.get("district_domain") or "").strip() or None,
    }


def get_session_district_slug() -> str | None:
    context = get_session_district_context()
    if not context:
        return None
    return str(context["district_slug"])


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


def apply_authenticated_session(user: dict[str, Any], access_token: str, district: DistrictContext) -> None:
    clear_non_auth_session_state()
    st.session_state["authenticated_user"] = district.email
    st.session_state["authenticated_user_id"] = district.user_id or str(user.get("id") or "").strip() or None
    st.session_state["supabase_access_token"] = access_token
    st.session_state["district_id"] = district.district_id
    st.session_state["district_slug"] = district.district_slug
    st.session_state["district_name"] = district.district_name
    st.session_state["district_domain"] = district.domain
    backfill_legacy_outputs(district.district_slug)
    st.query_params["session_token"] = access_token


def show_unlinked_district_message() -> None:
    st.error(UNLINKED_DISTRICT_MESSAGE)


def supabase_auth_request(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    supabase_url, anon_key = get_supabase_config()
    if not anon_key:
        raise RuntimeError("SUPABASE_ANON_KEY is not configured.")

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
        raise RuntimeError(str(message))

    return data


def sign_in_with_supabase(email: str, password: str) -> dict[str, Any]:
    return supabase_auth_request(
        "token?grant_type=password",
        {"email": email, "password": password},
    )


def create_supabase_account(email: str, password: str) -> dict[str, Any]:
    return supabase_auth_request(
        "signup",
        {
            "email": email,
            "password": password,
            "data": {
                "role": "appraiser",
                "allowed_app": "rendition_pilot",
            },
        },
    )


def get_supabase_user(access_token: str) -> dict[str, Any]:
    supabase_url, anon_key = get_supabase_config()
    if not anon_key:
        raise RuntimeError("SUPABASE_ANON_KEY is not configured.")

    response = requests.get(
        f"{supabase_url}/auth/v1/user",
        headers={
            "apikey": anon_key,
            "Authorization": f"Bearer {access_token}",
        },
        timeout=20,
    )
    try:
        data = response.json()
    except ValueError:
        data = {"message": response.text}

    if response.status_code >= 400:
        message = data.get("msg") or data.get("message") or "Could not restore Supabase session."
        raise RuntimeError(str(message))

    return data


def restore_login_from_query_params() -> None:
    if (
        st.session_state.get("authenticated_user")
        and st.session_state.get("supabase_access_token")
        and st.session_state.get("district_id")
    ):
        return

    access_token = st.query_params.get("session_token", "")
    if not access_token:
        return

    try:
        user = get_supabase_user(access_token)
    except Exception:
        st.query_params.clear()
        return

    try:
        district = build_district_context(user, access_token)
    except Exception:
        st.query_params.clear()
        return

    if not district:
        st.query_params.clear()
        st.session_state["district_link_missing"] = True
        return

    apply_authenticated_session(user, access_token, district)


def persist_login(user: dict[str, Any], access_token: str, district: DistrictContext) -> None:
    apply_authenticated_session(user, access_token, district)


def clear_login() -> None:
    for key in list(st.session_state.keys()):
        st.session_state.pop(key, None)
    st.query_params.clear()


def require_login() -> bool:
    restore_login_from_query_params()

    if st.session_state.pop("district_link_missing", False):
        show_unlinked_district_message()

    if (
        st.session_state.get("authenticated_user")
        and st.session_state.get("supabase_access_token")
        and st.session_state.get("district_id")
    ):
        with st.sidebar:
            st.caption(f"Signed in as {st.session_state['authenticated_user']}")
            st.caption(f"District: {st.session_state['district_name']}")
            if st.button("Sign Out", key="sign_out"):
                clear_login()
                st.rerun()
        return True

    st.markdown('<div class="ap-title">AppraisalPilot</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="ap-subtitle">Authorized rendition review access only.</div>',
        unsafe_allow_html=True,
    )

    _supabase_url, anon_key = get_supabase_config()
    if not anon_key:
        st.error("Supabase login is not configured. Set SUPABASE_ANON_KEY before running the app.")
        st.code('$env:SUPABASE_ANON_KEY="your-supabase-anon-key"', language="powershell")
        return False

    login_tab, create_tab, cad_tab = st.tabs(["Login", "Create Login", "New CAD Setup"])

    with login_tab:
        with st.form("login_form"):
            email = st.text_input("Email", value="", placeholder="name@lubbockcad.org", key="login_email").strip().lower()
            password = st.text_input("Password", value="", type="password", key="login_password")
            submitted = st.form_submit_button("Login")

        if submitted:
            try:
                auth_result = sign_in_with_supabase(email, password)
            except Exception as exc:
                st.error(f"Login failed: {exc}")
                return False

            access_token = auth_result.get("access_token")
            if not access_token:
                st.error("Login did not return a session. Confirm the account email first, then try again.")
                return False

            try:
                user = auth_result.get("user") or get_supabase_user(access_token)
                district = build_district_context(user, access_token)
            except DistrictServiceError as exc:
                st.error(str(exc))
                return False
            except Exception as exc:
                st.error(f"District lookup failed: {exc}")
                return False

            if not district:
                show_unlinked_district_message()
                return False

            persist_login(user, access_token, district)
            st.rerun()

    with create_tab:
        with st.form("create_login_form"):
            new_email = st.text_input("Email", value="", placeholder="name@cad.org", key="signup_email").strip().lower()
            new_password = st.text_input("Password", value="", type="password", key="signup_password")
            confirm_password = st.text_input("Confirm Password", value="", type="password", key="signup_confirm_password")
            create_submitted = st.form_submit_button("Create Login")

        if create_submitted:
            if len(new_password) < 8:
                st.error("Password must be at least 8 characters.")
                return False
            if new_password != confirm_password:
                st.error("Passwords do not match.")
                return False

            try:
                signup_result = create_supabase_account(new_email, new_password)
            except Exception as exc:
                st.error(f"Account creation failed: {exc}")
                return False

            access_token = (
                signup_result.get("access_token")
                or (signup_result.get("session") or {}).get("access_token")
            )
            user = signup_result.get("user")
            if access_token and not user:
                try:
                    user = get_supabase_user(access_token)
                except Exception:
                    user = {"email": new_email}

            if signup_result.get("session") or signup_result.get("access_token"):
                if access_token:
                    service_role_key = get_supabase_service_role_key()
                    email_domain = infer_domain_from_email(new_email)
                    if service_role_key and email_domain:
                        try:
                            domain_district = find_district_by_domain(
                                supabase_url=_supabase_url,
                                service_role_key=service_role_key,
                                domain=email_domain,
                            )
                            if domain_district:
                                link_user_to_district(
                                    supabase_url=_supabase_url,
                                    service_role_key=service_role_key,
                                    district_id=domain_district.district_id,
                                    email=new_email,
                                    user_id=str((user or {}).get("id") or "").strip() or None,
                                )
                        except Exception:
                            pass

                try:
                    district = build_district_context(user or {"email": new_email}, access_token)
                except DistrictServiceError as exc:
                    st.error(str(exc))
                    return False
                except Exception as exc:
                    st.error(f"District lookup failed: {exc}")
                    return False

                if not district:
                    show_unlinked_district_message()
                    return False

                persist_login(user or {"email": new_email}, access_token, district)
                st.rerun()
            else:
                st.success("Login created. Check your email if Supabase requires confirmation, then return to the Login tab.")

    with cad_tab:
        service_role_key = get_supabase_service_role_key()
        if not service_role_key:
            st.info("Set SUPABASE_SERVICE_ROLE_KEY to enable new CAD onboarding.")
        else:
            try:
                verify_supabase_district_setup(
                    supabase_url=_supabase_url,
                    service_role_key=service_role_key,
                )
            except DistrictServiceError as exc:
                st.error(str(exc))
                st.caption("Apply the migration in Supabase first, then reload this page.")
        with st.form("new_cad_form"):
            district_name = st.text_input("District Name", value="", placeholder="Example County Appraisal District", key="new_cad_name")
            district_slug_input = st.text_input("District Slug", value="", placeholder="example-cad", key="new_cad_slug")
            admin_email = st.text_input("Admin Email", value="", placeholder="name@cad.org", key="new_cad_email").strip().lower()
            new_password = st.text_input("Password", value="", type="password", key="new_cad_password")
            confirm_password = st.text_input("Confirm Password", value="", type="password", key="new_cad_confirm_password")
            cad_submitted = st.form_submit_button("Create District and Login")

        if cad_submitted:
            if not service_role_key:
                st.error("SUPABASE_SERVICE_ROLE_KEY is required for CAD onboarding.")
                return False
            try:
                verify_supabase_district_setup(
                    supabase_url=_supabase_url,
                    service_role_key=service_role_key,
                )
            except DistrictServiceError as exc:
                st.error(str(exc))
                return False
            if not district_name.strip():
                st.error("District name is required.")
                return False
            if not admin_email:
                st.error("Admin email is required.")
                return False
            if len(new_password) < 8:
                st.error("Password must be at least 8 characters.")
                return False
            if new_password != confirm_password:
                st.error("Passwords do not match.")
                return False

            district_slug = slugify_district_slug(district_slug_input) or slugify_district_name(district_name)
            district_domain = infer_domain_from_email(admin_email)

            try:
                district = create_or_update_district(
                    supabase_url=_supabase_url,
                    service_role_key=service_role_key,
                    name=district_name,
                    slug=district_slug,
                    domain=district_domain,
                )
            except Exception as exc:
                st.error(f"District setup failed: {exc}")
                return False

            try:
                signup_result = create_supabase_account(admin_email, new_password)
            except Exception as exc:
                st.error(f"Account creation failed: {exc}")
                return False

            access_token = (
                signup_result.get("access_token")
                or (signup_result.get("session") or {}).get("access_token")
            )
            user = signup_result.get("user")
            if access_token and not user:
                try:
                    user = get_supabase_user(access_token)
                except Exception:
                    user = {"email": admin_email}

            user_id = str((user or {}).get("id") or "").strip() or None

            try:
                link_user_to_district(
                    supabase_url=_supabase_url,
                    service_role_key=service_role_key,
                    district_id=district.district_id,
                    email=admin_email,
                    user_id=user_id,
                )
            except Exception as exc:
                st.error(f"District user linking failed: {exc}")
                return False

            if access_token:
                try:
                    linked_district = build_district_context(user or {"email": admin_email}, access_token)
                except Exception as exc:
                    st.error(f"District lookup failed: {exc}")
                    return False

                if not linked_district:
                    show_unlinked_district_message()
                    return False

                persist_login(user or {"email": admin_email}, access_token, linked_district)
                st.rerun()
            else:
                st.success("District created and user linked. Confirm the email if required, then sign in from the Login tab.")

    return False


def build_manual_override(
    mode: str,
    attachment_total,
    good_faith_value,
    historical_cost,
    acquisition_year,
    life_years,
    notes: str,
) -> dict | None:
    notes = notes or ""

    if mode == "Auto / Recommended":
        return None

    if mode == "Force Attachment Total":
        return {
            "attachment_total": float(attachment_total) if attachment_total is not None else None,
            "good_faith_value": None,
            "historical_cost": None,
            "acquisition_year": None,
            "life_years": None,
            "notes": notes,
        }

    if mode == "Force Good Faith Value":
        return {
            "attachment_total": None,
            "good_faith_value": float(good_faith_value) if good_faith_value is not None else None,
            "historical_cost": None,
            "acquisition_year": None,
            "life_years": None,
            "notes": notes,
        }

    if mode == "Force Historical Cost Less Depreciation":
        return {
            "attachment_total": None,
            "good_faith_value": None,
            "historical_cost": float(historical_cost) if historical_cost is not None else None,
            "acquisition_year": int(acquisition_year) if acquisition_year is not None else None,
            "life_years": int(life_years) if life_years is not None else None,
            "notes": notes,
        }

    return None


def format_money(value) -> str:
    if value is None or value == "":
        return "-"
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return str(value)


def format_percent(value) -> str:
    if value is None or value == "":
        return "-"
    try:
        return f"{float(value):.2%}"
    except (TypeError, ValueError):
        return str(value)


def format_text(value) -> str:
    if value is None or value == "":
        return "-"
    if isinstance(value, list):
        return " | ".join(str(v) for v in value) if value else "-"
    return str(value)


def parse_money_input(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


REVIEW_DECISION_LABELS = {
    "Accepted Recommended Value": "accepted",
    "Adjusted Value": "adjusted",
    "Closed": "closed",
    "No Assets": "no_assets",
}


def is_zero_value_decision(decision_code: str) -> bool:
    return decision_code in {"closed", "no_assets"}


def get_calculator_store_key(file_name: str) -> str:
    district_slug = get_session_district_slug() or "global"
    return f"saved_calculators_{district_slug}_{file_name}"


def get_calculator_editor_key(file_name: str) -> str:
    district_slug = get_session_district_slug() or "global"
    return f"calculator_editor_{district_slug}_{file_name}"


def get_calculator_cost_key(file_name: str, nonce: int, bucket: str) -> str:
    district_slug = get_session_district_slug() or "global"
    return f"calculator_cost_{district_slug}_{file_name}_{nonce}_{bucket}"


def get_saved_calculators(file_name: str) -> list[dict[str, Any]]:
    # Session state is the current persistence layer. This shape is ready for a future
    # Supabase-backed implementation without changing the rendering or payload contract.
    return list(st.session_state.get(get_calculator_store_key(file_name), []))


def set_saved_calculators(file_name: str, calculators: list[dict[str, Any]]) -> None:
    st.session_state[get_calculator_store_key(file_name)] = calculators


def build_default_calculator_editor(tax_year: int) -> dict[str, Any]:
    preset = SECTION_PRESETS["schedule_a_furniture"]
    return {
        "section_key": "schedule_a_furniture",
        "name": str(preset["label"]),
        "schedule": str(preset["schedule"]),
        "category": str(preset["category"]),
        "custom_name": "",
        "depreciation_table": str(preset["default_table"]),
        "tax_year": int(tax_year),
        "costs": {},
        "editing_id": None,
        "created_at": None,
        "nonce": 0,
    }


def get_calculator_editor(file_name: str, tax_year: int) -> dict[str, Any]:
    key = get_calculator_editor_key(file_name)
    editor = st.session_state.get(key)
    if editor is None:
        editor = build_default_calculator_editor(tax_year)
        st.session_state[key] = editor
    return editor


def save_calculator_editor(file_name: str, editor: dict[str, Any]) -> None:
    st.session_state[get_calculator_editor_key(file_name)] = editor


def reset_calculator_editor(file_name: str, tax_year: int) -> None:
    editor = build_default_calculator_editor(tax_year)
    previous = st.session_state.get(get_calculator_editor_key(file_name), {})
    editor["nonce"] = int(previous.get("nonce", 0)) + 1
    save_calculator_editor(file_name, editor)


def load_saved_calculator_into_editor(file_name: str, calculator: dict[str, Any]) -> None:
    schedule = str(calculator.get("schedule") or "Custom")
    category = str(calculator.get("category") or "").strip()
    section_key = "custom"
    for key, preset in SECTION_PRESETS.items():
        if preset["schedule"] == schedule and preset["category"] == category:
            section_key = key
            break
    editor = {
        "name": str(calculator.get("name") or ""),
        "section_key": section_key,
        "schedule": schedule,
        "category": category,
        "custom_name": "" if section_key != "custom" else str(calculator.get("name") or ""),
        "depreciation_table": str(calculator.get("depreciation_table") or "8_year"),
        "tax_year": int(resolve_tax_year(calculator.get("tax_year"))),
        "costs": {
            str(row.get("bucket")): float(row.get("cost", 0.0) or 0.0)
            for row in calculator.get("rows", []) or []
        },
        "editing_id": calculator.get("id"),
        "created_at": calculator.get("created_at"),
        "nonce": int(st.session_state.get(get_calculator_editor_key(file_name), {}).get("nonce", 0)) + 1,
    }
    save_calculator_editor(file_name, editor)


def apply_calculated_total_to_final_value(file_name: str, combined_total: float) -> None:
    st.session_state[f"final_value_{file_name}"] = f"{combined_total:.2f}"
    st.session_state[f"final_source_{file_name}"] = "calculator_combined_total"


def prettify_path(path: str | None) -> str:
    mapping = {
        "use_manual_attachment_total": "Manual Attachment Total",
        "use_manual_good_faith_value": "Manual Good Faith Value",
        "use_manual_historical_cost_depreciated": "Historical Cost Less Depreciation",
        "use_attachment_total_pending_review": "Attachment Total",
        "use_schedule_total_pending_review": "Schedule E Total",
        "use_good_faith_value_pending_review": "Good Faith Estimate",
        "calculator_combined_total": "Calculator Combined Total",
        "manual_review": "Manual Review",
    }
    if not path:
        return "-"
    return mapping.get(path, path)


def prettify_confidence(confidence: str | None) -> str:
    mapping = {
        "high": "High",
        "medium": "Medium",
        "low": "Low",
    }
    if not confidence:
        return "-"
    return mapping.get(confidence.lower(), confidence)


def confidence_color(confidence: str | None) -> str:
    confidence = (confidence or "").lower()
    if confidence == "high":
        return "#2ECC71"
    if confidence == "medium":
        return "#F1C40F"
    return "#E74C3C"


def get_status_label(result: dict) -> str:
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


def status_badge_html(result: dict) -> str:
    status = get_status_label(result)
    if status == "Manual Review":
        bg = "rgba(231, 76, 60, 0.15)"
        border = "#E74C3C"
        text = "🔴 Manual Review"
    elif status == "Review Recommended":
        bg = "rgba(241, 196, 15, 0.15)"
        border = "#F1C40F"
        text = "🟡 Review Recommended"
    else:
        bg = "rgba(46, 204, 113, 0.15)"
        border = "#2ECC71"
        text = "🟢 Ready"

    return f"""
    <div style="
        background:{bg};
        border:1px solid {border};
        border-radius:12px;
        padding:14px 16px;
        color:#FFFFFF;
        font-weight:700;
        margin-bottom:16px;
    ">
        {text}
    </div>
    """


def render_kv_section(title: str, items: list[tuple[str, str]]) -> None:
    st.subheader(title)
    rows = []
    for label, value in items:
        rows.append(
            f"""
            <div class="ap-kv-row">
                <div class="ap-kv-label">{label}</div>
                <div class="ap-kv-value">{value}</div>
            </div>
            """
        )
    st.markdown("".join(rows), unsafe_allow_html=True)


def show_top_metrics(result: dict) -> None:
    assessment = result.get("assessment_summary", {}) or {}
    value = (
        assessment.get("recommended_value")
        or assessment.get("recommended_market_value")
        or assessment.get("recommended_assessed_value")
        or assessment.get("extracted_value")
    )
    path = prettify_path(assessment.get("recommended_path"))
    confidence = prettify_confidence(assessment.get("confidence"))
    confidence_border = confidence_color(assessment.get("confidence"))
    reason = assessment.get("reason", "-")
    percent_good = (result.get("depreciated_override_result", {}) or {}).get("percent_good")
    extraction_provider = result.get("extraction_provider") or ((result.get("structured_extraction") or {}).get("extraction_provider"))

    st.markdown(
        f"""
        <div class="ap-decision-card">
            <div class="ap-decision-label">Recommended Value</div>
            <div class="ap-decision-value">{format_money(value)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if extraction_provider:
        st.caption(f"Extraction provider: {extraction_provider}")

    c1, c2, c3 = st.columns(3)

    with c1:
        st.markdown(
            f"""
            <div class="ap-mini-card" style="border:1px solid rgba(255,215,0,0.35);">
                <div class="ap-mini-label">Valuation Path</div>
                <div class="ap-mini-value">{path}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with c2:
        st.markdown(
            f"""
            <div class="ap-mini-card" style="border:1px solid rgba(255,215,0,0.35);">
                <div class="ap-mini-label">Percent Good</div>
                <div class="ap-mini-value">{format_percent(percent_good)}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with c3:
        st.markdown(
            f"""
            <div class="ap-mini-card" style="border:1px solid {confidence_border};">
                <div class="ap-mini-label" style="color:{confidence_border};">Confidence</div>
                <div class="ap-mini-value">{confidence}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    st.markdown(status_badge_html(result), unsafe_allow_html=True)

    st.markdown("### Decision Reason")
    st.markdown(
        f"""
        <div class="ap-reason-box">
            <strong>Decision:</strong><br><br>
            {reason if reason else "-"}
        </div>
        """,
        unsafe_allow_html=True,
    )


def show_flags_and_findings(result: dict) -> None:
    form_flags = result.get("form_flags", {}) or {}
    metadata = result.get("metadata", {}) or {}
    schedule_e = result.get("schedule_e", {}) or {}
    schedule_values = result.get("schedule_values", {}) or {}
    attachments = result.get("attachments", {}) or {}
    review_flags = result.get("review_flags", {}) or {}
    schema_review_flags = result.get("schema_review_flags", []) or []
    manual_override = result.get("manual_override", {}) or {}
    assessment = result.get("assessment_summary", {}) or {}
    depreciation = result.get("depreciated_override_result", {}) or {}
    structured = result.get("structured_extraction", {}) or {}
    schedule_breakdown = structured.get("schedule_breakdown", {}) or {}
    debug = structured.get("debug", {}) or {}
    normalized_schema = result.get("normalized_schema", {}) or {}

    left, right = st.columns(2)

    with left:
        st.markdown('<div class="ap-card">', unsafe_allow_html=True)
        render_kv_section(
            "Document Metadata",
            [
                ("Tax Year", format_text(metadata.get("tax_year"))),
                ("Owner Name", format_text(metadata.get("owner_name"))),
                ("Account Number", format_text(metadata.get("account_number"))),
                ("Signed Date", format_text(metadata.get("signed_date"))),
            ],
        )
        st.markdown("<br>", unsafe_allow_html=True)
        render_kv_section(
            "Form Flags",
            [
                ("Section 3 Present", "Yes" if form_flags.get("section_3_present") else "No"),
                ("Section 3 Prior-Year Box Checked", "Yes" if form_flags.get("section_3_prior_year_checked") else "No"),
                ("Section 5 Present", "Yes" if form_flags.get("section_5_present") else "No"),
                ("Over $20k Language", "Yes" if form_flags.get("section_5_over_20k_detected") else "No"),
                ("$125k Language", "Yes" if form_flags.get("section_5_125k_language_detected") else "No"),
                ("Section 5 Under $20k Checked", "Yes" if form_flags.get("section_5_under_20k_checked") else "No"),
                ("Section 5 $20k+ Checked", "Yes" if form_flags.get("section_5_20k_or_more_checked") else "No"),
                ("Section 5 $125k or Less Checked", "Yes" if form_flags.get("section_5_125k_or_less_checked") else "No"),
                ("Section 5 More Than $125k Checked", "Yes" if form_flags.get("section_5_more_than_125k_checked") else "No"),
                ("Signature Detected", "Yes" if form_flags.get("signature_block_detected") else "No"),
                ("SEE ATTACHED", "Yes" if form_flags.get("see_attached") else "No"),
            ],
        )
        st.markdown("<br>", unsafe_allow_html=True)
        render_kv_section(
            "Schedule / Attachments",
            [
                ("Extraction Provider", format_text(result.get("extraction_provider"))),
                ("Document Confidence", format_text(result.get("document_confidence"))),
                ("Schedule E Present", "Yes" if schedule_e.get("schedule_e_present") else "No"),
                ("Schedule E Total", format_money(schedule_e.get("total"))),
                ("Schedule A GFE Total", format_money(schedule_values.get("good_faith_total"))),
                ("Historical Cost Total", format_money(schedule_values.get("historical_cost_total"))),
                ("Schedule E M&E Present", "Yes" if schedule_e.get("machinery_and_equipment_present") else "No"),
                ("Attachment Summary Present", "Yes" if attachments.get("attachment_summary_present") else "No"),
                ("Best Attachment Total", format_money(attachments.get("best_attachment_total"))),
                ("Current Value Detected", "Yes" if attachments.get("current_value_detected") else "No"),
                ("Reported Cost Detected", "Yes" if attachments.get("reported_cost_detected") else "No"),
                ("Rendered Value Detected", "Yes" if attachments.get("rendered_value_detected") else "No"),
                ("Attachment M&E Present", "Yes" if attachments.get("machinery_and_equipment_present") else "No"),
            ],
        )
        st.markdown("</div>", unsafe_allow_html=True)

    with right:
        st.markdown('<div class="ap-card">', unsafe_allow_html=True)
        render_kv_section(
            "Override Inputs Used",
            [
                ("Attachment Total", format_money(manual_override.get("attachment_total"))),
                ("Good Faith Value", format_money(manual_override.get("good_faith_value"))),
                ("Historical Cost", format_money(manual_override.get("historical_cost"))),
                ("Acquisition Year", format_text(manual_override.get("acquisition_year"))),
                ("Life Years", format_text(manual_override.get("life_years"))),
                ("Notes", format_text(manual_override.get("notes"))),
            ],
        )
        st.markdown("<br>", unsafe_allow_html=True)
        render_kv_section(
            "Review / Decision Details",
            [
                ("Recommended Path", prettify_path(assessment.get("recommended_path"))),
                ("Value Source", format_text(assessment.get("value_source"))),
                ("Percent Good", format_percent(depreciation.get("percent_good"))),
                ("Confidence", prettify_confidence(assessment.get("confidence"))),
                ("Needs Manual Row Review", "Yes" if review_flags.get("needs_manual_row_review") else "No"),
                ("Needs Attachment Review", "Yes" if review_flags.get("needs_attachment_review") else "No"),
                ("Schema Review Flags", format_text(schema_review_flags)),
                ("Issues", format_text(assessment.get("issues"))),
            ],
        )
        st.markdown("</div>", unsafe_allow_html=True)

    st.markdown('<div class="ap-card">', unsafe_allow_html=True)
    st.subheader("Structured Extraction")
    breakdown_rows = []
    for schedule_key, section in schedule_breakdown.items():
        if not isinstance(section, dict):
            continue
        breakdown_rows.append(
            {
                "Schedule": schedule_key.replace("_", " ").title(),
                "Total": format_money(section.get("total")),
                "Confidence": section.get("confidence", "-"),
                "Rows": len(section.get("raw_rows") or []),
            }
        )
    if breakdown_rows:
        st.dataframe(pd.DataFrame(breakdown_rows), use_container_width=True, hide_index=True)
    render_kv_section(
        "Structured Debug",
        [
            ("Text Quality Score", format_text(debug.get("text_quality_score"))),
            ("Document AI Used", "Yes" if debug.get("document_ai_used") else "No"),
            ("Document AI Error", format_text(debug.get("document_ai_error"))),
            ("Missing Schedules", format_text(debug.get("missing_schedules"))),
            ("Low Confidence Sections", format_text(debug.get("low_confidence_sections"))),
            ("Document AI Env", format_text(debug.get("document_ai_env"))),
        ],
    )
    with st.expander("Normalized Extracted Data", expanded=False):
        st.json(normalized_schema)
    st.markdown("</div>", unsafe_allow_html=True)


def normalize_candidates_for_table(candidates: list[dict] | None) -> pd.DataFrame:
    candidates = candidates or []
    if not candidates:
        return pd.DataFrame(columns=["Page", "Label", "Value", "Score", "Context"])

    rows = []
    for c in candidates:
        rows.append(
            {
                "Page": c.get("page_number", "-"),
                "Field": c.get("field", c.get("label", "-")),
                "Label": c.get("label", c.get("rule", "-")),
                "Value": format_money(c.get("value")),
                "Score": c.get("score", c.get("confidence", "-")),
                "Source": c.get("source", "-"),
                "Evidence": c.get("evidence_text", c.get("context", c.get("source_text", "-"))),
            }
        )
    df = pd.DataFrame(rows)
    if "Score" in df.columns:
        try:
            df = df.sort_values(by="Score", ascending=False)
        except Exception:
            pass
    return df


def show_agent_review(result: dict) -> None:
    agent_review = result.get("agent_review", {}) or {}
    values = agent_review.get("recommended_values", {}) or {}

    st.markdown('<div class="ap-card">', unsafe_allow_html=True)
    render_kv_section(
        "AI / Fallback Review",
        [
            ("Status", format_text(agent_review.get("status"))),
            ("Selected Source", format_text(values.get("selected_source"))),
            ("Attachment Total", format_money(values.get("attachment_total"))),
            ("Good Faith Value", format_money(values.get("good_faith_value"))),
            ("Rendered Value", format_money(values.get("rendered_value"))),
            ("Historical Cost", format_money(values.get("historical_cost"))),
            ("Acquisition Year", format_text(values.get("acquisition_year"))),
            ("Confidence", format_text(agent_review.get("confidence"))),
            ("Flags", format_text(agent_review.get("review_flags"))),
        ],
    )
    st.markdown("### Review Reasoning")
    st.write(agent_review.get("reasoning") or agent_review.get("reason") or "-")
    rejected = agent_review.get("rejected_candidates") or {}
    if rejected:
        with st.expander("Rejected / Lower-Ranked Candidates", expanded=False):
            st.json(rejected)
    st.markdown("</div>", unsafe_allow_html=True)


def show_candidate_debug(result: dict) -> None:
    candidates = result.get("value_candidates", []) or []
    selected = result.get("selected_candidate") or {}

    st.markdown('<div class="ap-card">', unsafe_allow_html=True)
    st.subheader("Extraction Candidates")
    st.caption("Review extracted values and supporting evidence when needed.")

    c1, c2 = st.columns([1, 3])

    with c1:
        st.metric("Candidates Found", len(candidates))

    with c2:
        selected_label = selected.get("label", "-")
        selected_value = format_money(selected.get("value"))
        selected_page = selected.get("page_number", "-")
        selected_score = selected.get("score", "-")

        st.markdown(
            f"""
            <div class="ap-mini-card" style="border:1px solid rgba(255,215,0,0.35); min-height: auto;">
                <div class="ap-mini-label">Selected Candidate</div>
                <div style="color:#FFFFFF; font-size:1rem; line-height:1.7;">
                    <strong>Label:</strong> {selected_label}<br>
                    <strong>Value:</strong> {selected_value}<br>
                    <strong>Page:</strong> {selected_page}<br>
                    <strong>Score:</strong> {selected_score}
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    df = normalize_candidates_for_table(candidates)
    st.dataframe(df, use_container_width=True, hide_index=True)
    st.markdown("</div>", unsafe_allow_html=True)


@st.cache_data(show_spinner=False)
def render_pdf_pages(file_bytes: bytes) -> list[bytes]:
    pages: list[bytes] = []
    doc = fitz.open(stream=file_bytes, filetype="pdf")

    for page in doc:
        pix = page.get_pixmap(matrix=fitz.Matrix(1.4, 1.4), alpha=False)
        pages.append(pix.tobytes("png"))

    doc.close()
    return pages


def _preview_file_signature(file_bytes: bytes) -> str:
    return f"{len(file_bytes)}:{hash(file_bytes[:2048])}"


def _rotate_png_bytes(image_bytes: bytes, rotation_degrees: int) -> bytes:
    rotation = int(rotation_degrees) % 360
    if rotation == 0:
        return image_bytes

    with Image.open(BytesIO(image_bytes)) as image:
        rotated = image.rotate(-rotation, expand=True)
        output = BytesIO()
        rotated.save(output, format="PNG")
        return output.getvalue()


def show_pdf_preview(file_bytes: bytes) -> None:
    page_images = render_pdf_pages(file_bytes)

    if not page_images:
        st.warning("No PDF pages could be rendered.")
        return

    st.markdown(
        f'<div class="ap-toolbar-row"><div class="ap-toolbar-meta">{len(page_images)} page(s) rendered</div></div>',
        unsafe_allow_html=True,
    )

    file_signature = _preview_file_signature(file_bytes)
    signature_key = "single_pdf_preview_signature"
    page_key = "single_pdf_page_selector"
    rotation_key = "single_pdf_rotation_degrees"

    if st.session_state.get(signature_key) != file_signature:
        st.session_state[signature_key] = file_signature
        st.session_state[page_key] = 1
        st.session_state[rotation_key] = 0

    if len(page_images) == 1:
        current_rotation = int(st.session_state.get(rotation_key, 0) or 0)
        rotate_cols = st.columns([1, 1, 4])
        with rotate_cols[0]:
            if st.button("Left", key="single_pdf_rotate_left_single", use_container_width=True):
                st.session_state[rotation_key] = (current_rotation - 90) % 360
                st.rerun()
        with rotate_cols[1]:
            if st.button("Right", key="single_pdf_rotate_right_single", use_container_width=True):
                st.session_state[rotation_key] = (current_rotation + 90) % 360
                st.rerun()
        with rotate_cols[2]:
            st.caption(f"Rotation: {int(st.session_state.get(rotation_key, 0) or 0)} degrees")
        st.image(_rotate_png_bytes(page_images[0], int(st.session_state.get(rotation_key, 0) or 0)), use_container_width=True)
        return

    current_page = int(st.session_state.get(page_key, 1) or 1)
    current_rotation = int(st.session_state.get(rotation_key, 0) or 0)

    nav_cols = st.columns([1, 1, 1.25, 1, 1, 2.5])
    with nav_cols[0]:
        if st.button("Prev", key="single_pdf_prev_page", use_container_width=True, disabled=current_page <= 1):
            st.session_state[page_key] = max(1, current_page - 1)
            st.rerun()
    with nav_cols[1]:
        if st.button("Next", key="single_pdf_next_page", use_container_width=True, disabled=current_page >= len(page_images)):
            st.session_state[page_key] = min(len(page_images), current_page + 1)
            st.rerun()
    with nav_cols[2]:
        selected_page = st.selectbox(
            "Page",
            options=list(range(1, len(page_images) + 1)),
            format_func=lambda page_number: f"Page {page_number}",
            key=page_key,
            label_visibility="collapsed",
        )
    with nav_cols[3]:
        if st.button("Left", key="single_pdf_rotate_left", use_container_width=True):
            st.session_state[rotation_key] = (current_rotation - 90) % 360
            st.rerun()
    with nav_cols[4]:
        if st.button("Right", key="single_pdf_rotate_right", use_container_width=True):
            st.session_state[rotation_key] = (current_rotation + 90) % 360
            st.rerun()
    with nav_cols[5]:
        st.caption(f"{len(page_images)} pages | Page {int(selected_page)} of {len(page_images)} | Rotation {int(st.session_state.get(rotation_key, 0) or 0)} degrees")

    st.image(
        _rotate_png_bytes(page_images[int(selected_page) - 1], int(st.session_state.get(rotation_key, 0) or 0)),
        use_container_width=True,
    )


def run_pipeline_from_upload(file_name: str, file_bytes: bytes, manual_override: dict | None = None) -> dict:
    hydrate_analysis_env_from_secrets()

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(file_bytes)
        temp_pdf_path = Path(tmp.name)

    try:
        return run_rendition_pipeline(
            pdf_path=str(temp_pdf_path),
            manual_override=manual_override,
        )
    finally:
        try:
            temp_pdf_path.unlink(missing_ok=True)
        except Exception:
            pass


def get_result_value(result: dict) -> Any:
    assessment = result.get("assessment_summary", {}) or {}
    return (
        assessment.get("recommended_value")
        or assessment.get("recommended_market_value")
        or assessment.get("recommended_assessed_value")
        or assessment.get("extracted_value")
    )


def needs_manual_assist(result: dict) -> bool:
    assessment = result.get("assessment_summary", {}) or {}
    review_flags = result.get("review_flags", {}) or {}
    return bool(
        assessment.get("recommended_path") == "manual_review"
        or str(assessment.get("confidence") or "").lower() == "low"
        or get_result_value(result) is None
        or review_flags.get("low_text_extraction")
        or review_flags.get("ocr_unavailable")
    )


def extract_money_values(text: str) -> list[float]:
    values: list[float] = []
    pattern = re.compile(r"\$\s*\(?[0-9][0-9,\s]*(?:\.\d{1,2})?\)?|\b\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?\b|\b\d+\.\d{2}\b")
    for match in pattern.finditer(text or ""):
        value = parse_money_input(match.group(0))
        if value is not None and value > 0:
            values.append(value)
    return values


def calculate_depreciated_value(historical_cost: float, acquisition_year: int, life_years: int) -> tuple[float | None, float | None]:
    schedule_path = PROJECT_ROOT / "Data" / "depreciation_schedule.csv"
    if not schedule_path.exists():
        return None, None
    engine = DepreciationEngine(str(schedule_path))
    return engine.assess_value(
        original_cost=float(historical_cost),
        acquisition_year=int(acquisition_year),
        life_years=int(life_years),
    )


def apply_manual_assist_override(file_name: str, file_bytes: bytes, manual_override: dict) -> None:
    result = run_pipeline_from_upload(
        file_name=file_name,
        file_bytes=file_bytes,
        manual_override=manual_override,
    )
    st.session_state["single_result"] = result
    st.session_state["single_file_name"] = file_name
    st.session_state["single_file_bytes"] = file_bytes
    st.session_state.pop("single_locked_record", None)
    st.session_state.pop("single_saved_stamped_path", None)
    st.session_state.pop("single_saved_outputs", None)
    st.success("Manual value applied. Review the final value, then lock and save.")
    st.rerun()


def render_manual_assist_panel(file_name: str, result: dict, file_bytes: bytes) -> None:
    assessment = result.get("assessment_summary", {}) or {}
    expand_panel = needs_manual_assist(result)

    with st.expander("Manual Assist", expanded=expand_panel):
        if expand_panel:
            st.warning("Auto extraction is low confidence. Enter the value here and the app will still handle depreciation, summary, stamping, and queue output.")
        else:
            st.caption("Use this if the appraiser needs to override the extracted value.")

        tab_total, tab_good_faith, tab_historical = st.tabs([
            "Attachment Total",
            "Good Faith Sum",
            "Historical Cost",
        ])

        with tab_total:
            attachment_total = st.number_input(
                "Attachment Total",
                min_value=0.0,
                step=100.0,
                format="%.2f",
                key=f"assist_attachment_total_{file_name}",
            )
            notes = st.text_area(
                "Attachment Notes",
                value="",
                height=70,
                key=f"assist_attachment_notes_{file_name}",
            )
            if st.button("Apply Attachment Total", type="primary", key=f"assist_apply_attachment_{file_name}"):
                if attachment_total <= 0:
                    st.error("Enter an attachment total greater than zero.")
                else:
                    apply_manual_assist_override(
                        file_name,
                        file_bytes,
                        {
                            "attachment_total": float(attachment_total),
                            "good_faith_value": None,
                            "historical_cost": None,
                            "acquisition_year": None,
                            "life_years": None,
                            "notes": notes or "Manual assist attachment total.",
                        },
                    )

        with tab_good_faith:
            amount_text = st.text_area(
                "Good Faith Amounts",
                value="",
                height=140,
                placeholder="$4,500.00\n$9,000.00\n$30,250.00",
                key=f"assist_good_faith_amounts_{file_name}",
            )
            values = extract_money_values(amount_text)
            good_faith_total = round(sum(values), 2)
            c1, c2 = st.columns(2)
            with c1:
                st.metric("Line Items", len(values))
            with c2:
                st.metric("Good Faith Total", format_money(good_faith_total if values else None))
            notes = st.text_area(
                "Good Faith Notes",
                value="",
                height=70,
                key=f"assist_good_faith_notes_{file_name}",
            )
            if st.button("Apply Good Faith Sum", type="primary", key=f"assist_apply_good_faith_{file_name}"):
                if good_faith_total <= 0:
                    st.error("Enter one or more good faith amounts.")
                else:
                    apply_manual_assist_override(
                        file_name,
                        file_bytes,
                        {
                            "attachment_total": None,
                            "good_faith_value": good_faith_total,
                            "historical_cost": None,
                            "acquisition_year": None,
                            "life_years": None,
                            "notes": notes or f"Manual assist good faith sum from {len(values)} line item(s).",
                        },
                    )

        with tab_historical:
            default_year = min(max(datetime.now().year - 1, 1900), 2100)
            c1, c2, c3 = st.columns(3)
            with c1:
                historical_cost = st.number_input(
                    "Historical Cost",
                    min_value=0.0,
                    step=100.0,
                    format="%.2f",
                    key=f"assist_historical_cost_{file_name}",
                )
            with c2:
                acquisition_year = st.number_input(
                    "Acquisition Year",
                    min_value=1900,
                    max_value=2100,
                    step=1,
                    value=default_year,
                    key=f"assist_acquisition_year_{file_name}",
                )
            with c3:
                life_years = st.number_input(
                    "Life Years",
                    min_value=1,
                    max_value=50,
                    step=1,
                    value=5,
                    key=f"assist_life_years_{file_name}",
                )

            percent_good, depreciated_value = calculate_depreciated_value(
                historical_cost=historical_cost,
                acquisition_year=int(acquisition_year),
                life_years=int(life_years),
            )
            c4, c5 = st.columns(2)
            with c4:
                st.metric("Percent Good", format_percent(percent_good))
            with c5:
                st.metric("Depreciated Value", format_money(depreciated_value))

            notes = st.text_area(
                "Historical Cost Notes",
                value="",
                height=70,
                key=f"assist_historical_notes_{file_name}",
            )
            if st.button("Apply Historical Cost", type="primary", key=f"assist_apply_historical_{file_name}"):
                if historical_cost <= 0:
                    st.error("Enter historical cost greater than zero.")
                elif depreciated_value is None:
                    st.error("No depreciation schedule match found for that year/life.")
                else:
                    apply_manual_assist_override(
                        file_name,
                        file_bytes,
                        {
                            "attachment_total": None,
                            "good_faith_value": None,
                            "historical_cost": float(historical_cost),
                            "acquisition_year": int(acquisition_year),
                            "life_years": int(life_years),
                            "notes": notes or "Manual assist historical cost less depreciation.",
                        },
                    )


def render_rendition_calculator(file_name: str, result: dict) -> None:
    metadata = result.get("metadata", {}) or {}
    tax_year = resolve_tax_year(metadata.get("tax_year"))
    tables = load_depreciation_tables()
    editor = get_calculator_editor(file_name, tax_year)

    editor["tax_year"] = resolve_tax_year(editor.get("tax_year") or tax_year)
    editor["section_key"] = str(editor.get("section_key") or "schedule_a_furniture")
    preset = SECTION_PRESETS.get(editor["section_key"], SECTION_PRESETS["schedule_a_furniture"])
    editor["schedule"] = str(editor.get("schedule") or preset["schedule"])
    editor["category"] = str(editor.get("category") or preset["category"])
    editor["depreciation_table"] = str(editor.get("depreciation_table") or "8_year")
    editor["custom_name"] = str(editor.get("custom_name") or "")
    editor["name"] = str(editor.get("name") or "")
    editor["costs"] = dict(editor.get("costs") or {})
    save_calculator_editor(file_name, editor)

    section_key = f"calculator_section_{file_name}"
    custom_name_key = f"calculator_custom_name_{file_name}"
    name_key = f"calculator_name_{file_name}"
    table_key = f"calculator_table_{file_name}"
    tax_year_key = f"calculator_tax_year_{file_name}"

    if section_key not in st.session_state:
        st.session_state[section_key] = editor["section_key"]
    if custom_name_key not in st.session_state:
        st.session_state[custom_name_key] = editor["custom_name"]
    if name_key not in st.session_state:
        st.session_state[name_key] = editor["name"]
    if table_key not in st.session_state:
        st.session_state[table_key] = editor["depreciation_table"]
    if tax_year_key not in st.session_state:
        st.session_state[tax_year_key] = editor["tax_year"]

    st.markdown('<div class="ap-card ap-card-tight ap-workbench-pane">', unsafe_allow_html=True)
    st.markdown(
        """
        <div class="ap-section-head">
            <h4>Rendition Calculator</h4>
        </div>
        <div class="ap-calc-subtle">Compact appraisal worksheet. Enter costs while keeping the PDF visible.</div>
        """,
        unsafe_allow_html=True,
    )

    header_top_left, header_top_right = st.columns([1.25, 0.75], gap="small")
    with header_top_left:
        st.markdown('<div class="ap-calc-label">Section</div>', unsafe_allow_html=True)
        selected_section_key = st.selectbox(
            "Section",
            list(SECTION_PRESETS.keys()),
            format_func=lambda key: str(SECTION_PRESETS[key]["label"]),
            key=section_key,
            label_visibility="collapsed",
        )
    with header_top_right:
        st.markdown('<div class="ap-calc-label">Tax Year</div>', unsafe_allow_html=True)
        selected_tax_year = st.number_input(
            "Tax Year",
            min_value=2000,
            max_value=2100,
            step=1,
            key=tax_year_key,
            label_visibility="collapsed",
        )

    selected_preset = SECTION_PRESETS[selected_section_key]
    custom_name = ""
    if selected_section_key == "custom":
        st.markdown('<div class="ap-calc-label">Custom Section Name</div>', unsafe_allow_html=True)
        custom_name = st.text_input(
            "Custom Section Name",
            key=custom_name_key,
            placeholder="Schedule A - Leasehold Improvements",
            label_visibility="collapsed",
        ).strip()
    else:
        st.session_state[custom_name_key] = ""

    schedule = str(selected_preset["schedule"])
    category_value = str(selected_preset["category"])
    if selected_section_key == "custom":
        category_value = custom_name or "Custom"

    generated_name = custom_name if selected_section_key == "custom" and custom_name else generate_calculator_name(schedule, category_value)
    if not st.session_state.get(name_key):
        st.session_state[name_key] = generated_name

    st.markdown('<div class="ap-calc-label">Calculator Name</div>', unsafe_allow_html=True)
    calculator_name = st.text_input(
        "Calculator Name",
        key=name_key,
        placeholder=generated_name,
        label_visibility="collapsed",
    ).strip()

    if table_key not in st.session_state or editor.get("section_key") != selected_section_key:
        st.session_state[table_key] = str(selected_preset["default_table"])

    st.markdown('<div class="ap-calc-label">Depreciation Table</div>', unsafe_allow_html=True)
    depreciation_table = st.selectbox(
        "Depreciation Table",
        list(TABLE_METADATA.keys()),
        format_func=lambda key: TABLE_METADATA[key]["label"],
        key=table_key,
        label_visibility="collapsed",
    )

    editor["section_key"] = selected_section_key
    editor["schedule"] = schedule
    editor["category"] = category_value
    editor["custom_name"] = custom_name
    editor["name"] = calculator_name
    editor["depreciation_table"] = depreciation_table
    editor["tax_year"] = int(selected_tax_year)

    rows = build_calculator_rows(
        depreciation_table,
        int(selected_tax_year),
        costs=editor["costs"],
        tables=tables,
    )

    st.markdown(
        """
        <div class="ap-calc-table-head">
            <div>Year</div>
            <div>Cost</div>
            <div>Factor</div>
            <div style="text-align:right;">Value</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    rows_container = st.container(height=360, border=False)
    with rows_container:
        for row in rows:
            cost_input_key = get_calculator_cost_key(file_name, int(editor["nonce"]), row["bucket"])
            if cost_input_key not in st.session_state:
                st.session_state[cost_input_key] = float(editor["costs"].get(row["bucket"], 0.0) or 0.0)

            row_cols = st.columns([0.65, 1.3, 0.8, 0.95], gap="small")
            row_cols[0].markdown(f'<div class="ap-calc-year">{row["display_year"]}</div>', unsafe_allow_html=True)
            cost_value = row_cols[1].number_input(
                f"Cost {row['display_year']}",
                min_value=0.0,
                step=100.0,
                format="%.2f",
                key=cost_input_key,
                label_visibility="collapsed",
            )
            editor["costs"][row["bucket"]] = round(float(cost_value), 2)
            row["cost"] = editor["costs"][row["bucket"]]
            row["value"] = round(row["cost"] * row["factor"], 2)
            row_cols[2].markdown(f'<span class="ap-calc-factor">{row["factor"]:.2f}</span>', unsafe_allow_html=True)
            row_cols[3].markdown(f'<div class="ap-calc-value">{format_money(row["value"])}</div>', unsafe_allow_html=True)

    section_total = calculate_section_total(rows)
    save_calculator_editor(file_name, editor)

    st.markdown('<div class="ap-calc-footer">', unsafe_allow_html=True)
    footer_cols = st.columns([0.8, 1.15, 1.0], gap="small")
    with footer_cols[0]:
        st.markdown('<div class="ap-calc-total-label">Total</div>', unsafe_allow_html=True)
        st.markdown(f'<div class="ap-calc-total">{format_money(section_total)}</div>', unsafe_allow_html=True)
    with footer_cols[1]:
        if st.button("Save Section", type="primary", key=f"save_calculator_{file_name}", use_container_width=True):
            if selected_section_key == "custom" and not (custom_name or calculator_name):
                st.error("Enter a custom section name before saving this calculator.")
            else:
                saved_calculators = get_saved_calculators(file_name)
                saved_name = calculator_name or generated_name
                saved_calculator = build_saved_calculator(
                    name=saved_name,
                    schedule=schedule,
                    category=category_value,
                    depreciation_table=depreciation_table,
                    tax_year=int(selected_tax_year),
                    rows=rows,
                    calculator_id=editor.get("editing_id"),
                    created_at=editor.get("created_at"),
                )

                updated = False
                for index, existing in enumerate(saved_calculators):
                    if existing.get("id") == saved_calculator["id"]:
                        saved_calculators[index] = saved_calculator
                        updated = True
                        break
                if not updated:
                    saved_calculators.append(saved_calculator)

                set_saved_calculators(file_name, saved_calculators)
                reset_calculator_editor(file_name, int(selected_tax_year))
                for widget_key in [section_key, custom_name_key, name_key, table_key, tax_year_key]:
                    st.session_state.pop(widget_key, None)
                st.success(f"Saved {saved_name}.")
                st.rerun()
    with footer_cols[2]:
        if st.button("New Section", key=f"new_calculator_{file_name}", use_container_width=True):
            reset_calculator_editor(file_name, int(selected_tax_year))
            for widget_key in [section_key, custom_name_key, name_key, table_key, tax_year_key]:
                st.session_state.pop(widget_key, None)
            st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)

    if editor.get("editing_id"):
        st.caption("Editing an existing saved section.")

    saved_calculators = get_saved_calculators(file_name)
    combined_total = calculate_combined_total(saved_calculators)
    st.markdown('<div class="ap-saved-work-card">', unsafe_allow_html=True)
    st.markdown('<div class="ap-saved-work-title">Saved Work</div>', unsafe_allow_html=True)
    st.metric("Calculated Total Value", format_money(combined_total))
    if st.button(
        "Use Calculated Total as Final Value",
        key=f"use_calculated_total_{file_name}",
        use_container_width=True,
    ):
        apply_calculated_total_to_final_value(file_name, combined_total)
        st.success("Final value populated from saved calculator totals.")
        st.rerun()

    if not saved_calculators:
        st.caption("No saved calculator sections yet.")
    else:
        with st.expander(f"Saved sections ({len(saved_calculators)})", expanded=False):
            for calculator in saved_calculators:
                label = (
                    f"{calculator.get('name')} | "
                    f"{format_money(calculator.get('section_total'))}"
                )
                with st.expander(label, expanded=False):
                    st.caption(
                        f"Schedule {calculator.get('schedule')} | Tax Year {calculator.get('tax_year')} | "
                        f"{TABLE_METADATA.get(calculator.get('depreciation_table'), {}).get('label', calculator.get('depreciation_table'))}"
                    )
                    review_rows = [
                        {
                            "Year": row.get("display_year"),
                            "Cost": format_money(row.get("cost")),
                            "Factor": f"{float(row.get('factor', 0.0)):.2f}",
                            "Value": format_money(row.get("value")),
                        }
                        for row in calculator.get("rows", []) or []
                    ]
                    st.dataframe(pd.DataFrame(review_rows), use_container_width=True, hide_index=True)
                    summary_cols = st.columns(2, gap="small")
                    with summary_cols[0]:
                        if st.button(
                            "Edit",
                            key=f"edit_saved_calculator_{file_name}_{calculator.get('id')}",
                            use_container_width=True,
                        ):
                            load_saved_calculator_into_editor(file_name, calculator)
                            for widget_key in [section_key, custom_name_key, name_key, table_key, tax_year_key]:
                                st.session_state.pop(widget_key, None)
                            st.rerun()
                    with summary_cols[1]:
                        if st.button(
                            "Delete",
                            key=f"delete_saved_calculator_{file_name}_{calculator.get('id')}",
                            use_container_width=True,
                        ):
                            remaining = [
                                item for item in saved_calculators
                                if item.get("id") != calculator.get("id")
                            ]
                            set_saved_calculators(file_name, remaining)
                            if editor.get("editing_id") == calculator.get("id"):
                                reset_calculator_editor(file_name, tax_year)
                            st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)


def reset_single_review_state() -> None:
    file_name = st.session_state.get("single_file_name")
    st.session_state["single_upload_reset_counter"] = (
        int(st.session_state.get("single_upload_reset_counter", 0)) + 1
    )
    for key in [
        "single_result",
        "single_file_name",
        "single_file_bytes",
        "single_locked_record",
        "single_saved_stamped_path",
        "single_saved_stamped_bytes",
        "single_saved_stamped_name",
        "single_saved_outputs",
        "single_download_stamped_bytes",
        "single_download_stamped_name",
        "single_notes",
    ]:
        st.session_state.pop(key, None)

    if file_name:
        for prefix in [
            "saved_calculators_",
            "calculator_editor_",
            "calculator_section_",
            "calculator_custom_name_",
            "calculator_name_",
            "calculator_table_",
            "calculator_tax_year_",
            "final_value_",
            "final_source_",
        ]:
            st.session_state.pop(f"{prefix}{file_name}", None)


def finalize_review_panel(file_name: str, result: dict, file_bytes: bytes) -> None:
    recommended_value = get_recommended_value(result)
    default_source = (result.get("assessment_summary", {}) or {}).get("value_source") or "pipeline_recommendation"
    metadata = result.get("metadata", {}) or {}
    combined_total = calculate_combined_total(get_saved_calculators(file_name))

    final_value_key = f"final_value_{file_name}"
    final_source_key = f"final_source_{file_name}"
    if final_value_key not in st.session_state:
        st.session_state[final_value_key] = "" if recommended_value is None else str(recommended_value)
    if final_source_key not in st.session_state:
        st.session_state[final_source_key] = default_source
    decision_key = f"final_decision_{file_name}"
    if decision_key not in st.session_state:
        st.session_state[decision_key] = "Accepted Recommended Value"

    st.markdown('<div class="ap-card">', unsafe_allow_html=True)
    st.subheader("Finalize Review")
    st.caption("Confirm the final value, enter initials/account, then lock and save the reviewed rendition.")

    if combined_total:
        current_final_value = parse_money_input(st.session_state.get(final_value_key))
        st.markdown(
            f"""
            <div class="ap-reason-box">
                <strong>Calculated Total:</strong> {format_money(combined_total)}<br>
                <strong>Final Appraiser Value:</strong> {format_money(current_final_value)}
            </div>
            """,
            unsafe_allow_html=True,
        )

    shortcut_1, shortcut_2 = st.columns(2)
    with shortcut_1:
        if st.button("Mark Closed", key=f"mark_closed_{file_name}", use_container_width=True):
            st.session_state[decision_key] = "Closed"
            st.session_state[final_value_key] = "0"
            st.session_state[final_source_key] = "closed_account"
            st.rerun()
    with shortcut_2:
        if st.button("Mark No Assets", key=f"mark_no_assets_{file_name}", use_container_width=True):
            st.session_state[decision_key] = "No Assets"
            st.session_state[final_value_key] = "0"
            st.session_state[final_source_key] = "no_assets_reported"
            st.rerun()

    c1, c2 = st.columns([1, 1])
    with c1:
        final_value_text = st.text_input(
            "Final Value",
            key=final_value_key,
        )
    with c2:
        final_source = st.selectbox(
            "Final Source",
            list(dict.fromkeys([
                default_source,
                "calculator_combined_total",
                "manual_override",
                "attachment_total",
                "good_faith_value",
                "historical_cost_depreciated",
                "schedule_e_total",
                "agent_review",
                "manual_review",
                "closed_account",
                "no_assets_reported",
            ])),
            key=final_source_key,
        )

    c3, c4 = st.columns([1, 1])
    with c3:
        appraiser_initials = st.text_input(
            "Appraiser Initials",
            value="",
            max_chars=12,
            key=f"final_initials_{file_name}",
        )
    with c4:
        decision = st.radio(
            "Review Decision",
            list(REVIEW_DECISION_LABELS.keys()),
            horizontal=True,
            key=decision_key,
        )

    account_number = st.text_input(
        "Appraisal District Account / P#",
        value=str(metadata.get("account_number") or ""),
        placeholder="Example: P164755",
        key=f"final_account_number_{file_name}",
    )

    final_notes = st.text_area(
        "Appraiser Notes",
        value="",
        key=f"final_notes_{file_name}",
        height=90,
    )

    locked_record = st.session_state.get("single_locked_record")
    if locked_record:
        locked_decision_label = get_decision_label(locked_record.get("decision"))
        st.success(
            f"Locked {locked_decision_label} for {locked_record.get('account_number') or account_number} "
            f"at {format_money(locked_record.get('final_value'))}."
        )
        download_name = f"{locked_record.get('account_number') or Path(file_name).stem}.pdf"
        if not st.session_state.get("single_download_stamped_bytes"):
            try:
                preview_pdf = stamp_reviewed_pdf(
                    file_name=file_name,
                    file_bytes=file_bytes,
                    final_record=locked_record,
                )
                st.session_state["single_download_stamped_name"] = preview_pdf.name
                st.session_state["single_download_stamped_bytes"] = preview_pdf.read_bytes()
            except Exception as exc:
                st.error(f"Could not create stamped PDF download: {exc}")
        if st.session_state.get("single_download_stamped_bytes"):
            st.download_button(
                "Save Stamped Rendition to My Computer",
                data=st.session_state["single_download_stamped_bytes"],
                file_name=st.session_state.get("single_download_stamped_name") or download_name,
                mime="application/pdf",
                type="primary",
                use_container_width=True,
                key=f"download_locked_stamped_{file_name}",
            )

    saved_path = st.session_state.get("single_saved_stamped_path")
    if saved_path:
        st.success(f"Stamped rendition saved to {saved_path}")
        saved_outputs = st.session_state.get("single_saved_outputs")
        if saved_outputs:
            st.caption(saved_outputs)
        saved_bytes = st.session_state.get("single_saved_stamped_bytes")
        saved_name = st.session_state.get("single_saved_stamped_name") or "stamped_rendition.pdf"
        if saved_bytes:
            st.download_button(
                "Download Stamped Rendition",
                data=saved_bytes,
                file_name=saved_name,
                mime="application/pdf",
                use_container_width=True,
                key=f"download_stamped_{file_name}",
            )
        if st.button("Next Account", key=f"next_account_{file_name}", use_container_width=True):
            reset_single_review_state()
            st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)
        return

    lock_final, save_rendition = st.columns(2)
    with lock_final:
        if st.button("Lock Final Value", type="primary", key=f"lock_review_{file_name}", use_container_width=True):
            decision_code = REVIEW_DECISION_LABELS.get(decision, "adjusted")
            final_value = parse_money_input(final_value_text)
            if final_value is None and is_zero_value_decision(decision_code):
                final_value = 0.0
            if final_value is None:
                st.error("Enter a valid final value before locking.")
            elif not appraiser_initials.strip():
                st.error("Enter appraiser initials before locking.")
            elif not account_number.strip():
                st.error("Enter the appraisal district account / P# before locking.")
            else:
                record = build_final_review_record(
                    file_name=file_name,
                    result=result,
                    final_value=final_value,
                    final_source=final_source,
                    appraiser_notes=final_notes,
                    appraiser_initials=appraiser_initials.strip().upper(),
                    account_number=account_number.strip().upper(),
                    decision=decision_code,
                    district_context=get_session_district_context(),
                )
                record["saved_calculators"] = get_saved_calculators(file_name)
                record["calculated_total_value"] = combined_total
                st.session_state.pop("single_download_stamped_bytes", None)
                st.session_state.pop("single_download_stamped_name", None)
                st.session_state["single_locked_record"] = record
                st.success("Final value locked. Click Save Rendition to create the stamped upload PDF.")
                st.rerun()

    with save_rendition:
        if st.button("Save Rendition", key=f"save_rendition_{file_name}", use_container_width=True):
            locked_record = st.session_state.get("single_locked_record")
            if not locked_record:
                st.error("Lock the final value before saving the rendition.")
            else:
                try:
                    stamped_pdf = stamp_reviewed_pdf(
                        file_name=file_name,
                        file_bytes=file_bytes,
                        final_record=locked_record,
                        district_slug=get_session_district_slug(),
                    )
                    locked_record["stamped_pdf"] = str(stamped_pdf)
                    paths = save_review_outputs(
                        file_name=file_name,
                        result=result,
                        final_record=locked_record,
                        district_context=get_session_district_context(),
                    )
                    append_queue_row(
                        file_name=file_name,
                        result={**result, "final_review": locked_record},
                        status="Locked",
                        district_context=get_session_district_context(),
                    )
                except Exception as exc:
                    st.error(f"Could not save stamped rendition: {exc}")
                else:
                    st.session_state["single_saved_stamped_path"] = str(stamped_pdf)
                    st.session_state["single_saved_stamped_name"] = stamped_pdf.name
                    st.session_state["single_saved_stamped_bytes"] = stamped_pdf.read_bytes()
                    st.session_state["single_saved_outputs"] = " | ".join(
                        str(path) for path in {**paths, "stamped_pdf": stamped_pdf}.values()
                    )
                    st.rerun()

    st.markdown("</div>", unsafe_allow_html=True)


def build_batch_row(file_name: str, result: dict) -> dict:
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
    issues_text = " | ".join(issues) if issues else "-"

    return {
        "File Name": file_name,
        "Tax Year": metadata.get("tax_year") or "-",
        "Owner Name": metadata.get("owner_name") or "-",
        "Account Number": metadata.get("account_number") or "-",
        "Recommended Value": format_money(value),
        "Extraction Provider": result.get("extraction_provider") or "-",
        "Valuation Path": prettify_path(assessment.get("recommended_path")),
        "Confidence": prettify_confidence(assessment.get("confidence")),
        "Status": get_status_label(result),
        "Value Source": assessment.get("value_source") or "-",
        "Signature Detected": bool(form_flags.get("signature_block_detected")),
        "SEE ATTACHED": bool(form_flags.get("see_attached")),
        "Schedule E Total": format_money(schedule_e.get("total")),
        "Schedule A GFE Total": format_money(schedule_values.get("good_faith_total")),
        "Historical Cost Total": format_money(schedule_values.get("historical_cost_total")),
        "Attachment Total": format_money(attachments.get("best_attachment_total")),
        "Agent Status": agent_review.get("status") or "-",
        "Agent Flags": " | ".join(str(x) for x in agent_review.get("review_flags", []) or []) or "-",
        "Candidates Found": len(result.get("value_candidates", []) or []),
        "Needs Manual Row Review": bool(review_flags.get("needs_manual_row_review")),
        "Needs Attachment Review": bool(review_flags.get("needs_attachment_review")),
        "Issues": issues_text,
    }


def render_single_review() -> None:
    st.markdown('<div class="ap-card">', unsafe_allow_html=True)
    st.subheader("Single Review Controls")
    upload_key = f"single_upload_{st.session_state.get('single_upload_reset_counter', 0)}"

    c1, c2, c3 = st.columns([1.7, 1.1, 0.55])

    with c1:
        uploaded_file = st.file_uploader(
            "Upload rendition PDF",
            type=["pdf"],
            accept_multiple_files=False,
            key=upload_key,
        )

    with c2:
        mode = st.selectbox(
            "Valuation Mode",
            [
                "Auto / Recommended",
                "Force Attachment Total",
                "Force Good Faith Value",
                "Force Historical Cost Less Depreciation",
            ],
            key="single_mode",
        )

    with c3:
        st.markdown("<div style='height: 28px;'></div>", unsafe_allow_html=True)
        run_review = st.button("Run Review", type="primary", use_container_width=True, key="single_run_review")

    attachment_total = None
    good_faith_value = None
    historical_cost = None
    acquisition_year = None
    life_years = None

    dynamic_cols = st.columns(4)

    if mode == "Force Attachment Total":
        with dynamic_cols[0]:
            attachment_total = st.number_input(
                "Attachment Total",
                min_value=0.0,
                step=100.0,
                format="%.2f",
                key="single_attachment_total",
            )

    if mode == "Force Good Faith Value":
        with dynamic_cols[0]:
            good_faith_value = st.number_input(
                "Good Faith Value",
                min_value=0.0,
                step=100.0,
                format="%.2f",
                key="single_good_faith_value",
            )

    if mode == "Force Historical Cost Less Depreciation":
        with dynamic_cols[0]:
            historical_cost = st.number_input(
                "Historical Cost",
                min_value=0.0,
                step=100.0,
                format="%.2f",
                key="single_historical_cost",
            )
        with dynamic_cols[1]:
            acquisition_year = st.number_input(
                "Acquisition Year",
                min_value=1900,
                max_value=2100,
                step=1,
                value=2022,
                key="single_acquisition_year",
            )
        with dynamic_cols[2]:
            life_years = st.number_input(
                "Life Years",
                min_value=1,
                max_value=50,
                step=1,
                value=5,
                key="single_life_years",
            )

    st.markdown("</div>", unsafe_allow_html=True)

    if not uploaded_file:
        st.info("Upload a rendition PDF to begin.")
        return

    file_bytes = uploaded_file.getvalue()

    if run_review:
        manual_override = build_manual_override(
            mode=mode,
            attachment_total=attachment_total,
            good_faith_value=good_faith_value,
            historical_cost=historical_cost,
            acquisition_year=acquisition_year,
            life_years=life_years,
            notes="",
        )

        result = run_pipeline_from_upload(
            file_name=uploaded_file.name,
            file_bytes=file_bytes,
            manual_override=manual_override,
        )
        st.session_state["single_result"] = result
        st.session_state["single_file_name"] = uploaded_file.name
        st.session_state["single_file_bytes"] = file_bytes

        st.success("Review completed.")

    result = st.session_state.get("single_result")
    result_file_name = st.session_state.get("single_file_name", uploaded_file.name)
    result_file_bytes = st.session_state.get("single_file_bytes", file_bytes)

    st.markdown('<div class="ap-card">', unsafe_allow_html=True)
    st.subheader("Analysis")
    st.markdown(
        '<div class="ap-muted">Focus on Recommended Value, Valuation Path, Confidence, extraction source, and reason first.</div>',
        unsafe_allow_html=True,
    )
    if result:
        show_top_metrics(result)
    else:
        st.info("Run Review to populate the analysis summary.")
    st.markdown("</div>", unsafe_allow_html=True)

    left_col, right_col = st.columns([1.08, 0.92], gap="medium")

    with left_col:
        st.markdown('<div class="ap-card ap-card-tight">', unsafe_allow_html=True)
        st.markdown(
            """
            <div class="ap-section-head">
                <h3>Rendition PDF</h3>
            </div>
            """,
            unsafe_allow_html=True,
        )
        button_cols = st.columns([1, 1.25])
        with button_cols[0]:
            st.download_button(
                "Download PDF",
                data=file_bytes,
                file_name=uploaded_file.name,
                mime="application/pdf",
                use_container_width=True,
                key="single_download_pdf",
            )
        with button_cols[1]:
            st.caption(f"File: {uploaded_file.name}")
        show_pdf_preview(file_bytes)
        st.markdown("</div>", unsafe_allow_html=True)

    with right_col:
        if result:
            render_rendition_calculator(result_file_name, result)
        else:
            st.markdown('<div class="ap-card ap-card-tight">', unsafe_allow_html=True)
            st.subheader("Rendition Calculator")
            st.info("Run Review first, then use the calculator beside the PDF.")
            st.markdown("</div>", unsafe_allow_html=True)

    if result:
        assist_col, finalize_col = st.columns([0.9, 1.1])
        with assist_col:
            render_manual_assist_panel(result_file_name, result, result_file_bytes)
        with finalize_col:
            finalize_review_panel(result_file_name, result, result_file_bytes)

        with st.expander("Document / Form / Schedule Details", expanded=False):
            show_flags_and_findings(result)

        with st.expander("AI Review / Reasoning", expanded=False):
            show_agent_review(result)

        with st.expander("Extracted Value Evidence", expanded=False):
            show_candidate_debug(result)

        with st.expander("One-Page Summary", expanded=False):
            st.code(build_cli_summary(result=result, source_path=result_file_name), language="text")

        with st.expander("Technical JSON", expanded=False):
            st.json(result)

        st.download_button(
            "Download JSON Result",
            data=json.dumps(result, indent=2, default=str),
            file_name=f"{result_file_name.rsplit('.', 1)[0]}_review.json",
            mime="application/json",
            use_container_width=True,
            key="single_download_json",
        )


def render_batch_review() -> None:
    st.markdown('<div class="ap-card">', unsafe_allow_html=True)
    st.subheader("Batch Review Controls")
    uploaded_files = st.file_uploader(
        "Upload multiple rendition PDFs",
        type=["pdf"],
        accept_multiple_files=True,
        key="batch_upload",
    )
    run_batch = st.button("Run Batch Review", type="primary", use_container_width=False, key="batch_run")
    st.markdown("</div>", unsafe_allow_html=True)

    st.markdown('<div class="ap-card">', unsafe_allow_html=True)
    st.subheader("Batch Review")
    st.markdown(
        '<div class="ap-muted">Upload multiple PDFs, run the pipeline on all of them, and use the table to spot outliers fast.</div>',
        unsafe_allow_html=True,
    )

    if not uploaded_files:
        st.info("Upload one or more PDFs to begin batch review.")
        st.markdown("</div>", unsafe_allow_html=True)
        return

    st.caption(f"{len(uploaded_files)} file(s) ready")

    if run_batch:
        rows: list[dict] = []
        results_payload: dict[str, dict] = {}

        progress_bar = st.progress(0)
        status_text = st.empty()

        total = len(uploaded_files)

        for idx, uploaded_file in enumerate(uploaded_files, start=1):
            status_text.write(f"Processing {idx} of {total}: {uploaded_file.name}")
            file_bytes = uploaded_file.getvalue()

            result = run_pipeline_from_upload(
                file_name=uploaded_file.name,
                file_bytes=file_bytes,
                manual_override=None,
            )

            rows.append(build_batch_row(uploaded_file.name, result))
            results_payload[uploaded_file.name] = result
            save_review_outputs(
                file_name=uploaded_file.name,
                result=result,
                district_context=get_session_district_context(),
            )
            append_queue_row(
                file_name=uploaded_file.name,
                result=result,
                status=get_status_label(result),
                district_context=get_session_district_context(),
            )
            progress_bar.progress(idx / total)

        status_text.success("Batch review completed.")

        df = pd.DataFrame(rows)
        st.dataframe(df, use_container_width=True, hide_index=True)

        d1, d2 = st.columns(2)
        with d1:
            st.download_button(
                "Download Batch Results JSON",
                data=json.dumps(results_payload, indent=2, default=str),
                file_name="batch_review_results.json",
                mime="application/json",
                use_container_width=True,
                key="batch_download_json",
            )
        with d2:
            st.download_button(
                "Download Batch Results CSV",
                data=df.to_csv(index=False).encode("utf-8"),
                file_name="batch_review_results.csv",
                mime="text/csv",
                use_container_width=True,
                key="batch_download_csv",
            )

        with st.expander("Raw Batch JSON", expanded=False):
            st.json(results_payload)
    else:
        st.info("Click Run Batch Review to process all uploaded files.")
    st.markdown("</div>", unsafe_allow_html=True)


def render_review_queue() -> None:
    district_context = get_session_district_context()
    district_slug = get_session_district_slug()
    backfill_legacy_outputs(district_slug)
    paths = ensure_output_dirs(district_slug)
    st.markdown('<div class="ap-card">', unsafe_allow_html=True)
    st.subheader("Review Queue")
    st.caption(f"Saved outputs are written to {paths['root']}")

    if paths["queue_csv"].exists():
        try:
            df = pd.read_csv(paths["queue_csv"])
            st.dataframe(df.sort_values(by="processed_at", ascending=False), use_container_width=True, hide_index=True)
            st.download_button(
                "Download Queue CSV",
                data=paths["queue_csv"].read_bytes(),
                file_name=f"{(district_context or {}).get('district_slug') or 'district'}_review_queue.csv",
                mime="text/csv",
                use_container_width=True,
                key="queue_download_csv",
            )
        except Exception as exc:
            st.error(f"Could not read review queue: {exc}")
    else:
        st.info("No saved review queue yet. Run a single or batch review and save/lock it.")

    st.markdown("</div>", unsafe_allow_html=True)

    st.markdown('<div class="ap-card">', unsafe_allow_html=True)
    st.subheader("Completed Reviews")
    completed_files = sorted(paths["completed"].glob("*_final.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not completed_files:
        st.info("No locked final reviews yet.")
    else:
        rows = []
        for path in completed_files:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                data = {}
            rows.append(
                {
                    "File": data.get("file_name", path.name),
                    "Final Value": format_money(data.get("final_value")),
                    "Final Source": data.get("final_source", "-"),
                    "Locked At": data.get("locked_at", "-"),
                    "Notes": data.get("appraiser_notes", ""),
                }
            )
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    st.markdown("</div>", unsafe_allow_html=True)


def main() -> None:
    if not require_login():
        return

    district_context = get_session_district_context()
    backfill_legacy_outputs(get_session_district_slug())

    st.markdown('<div class="ap-title">AppraisalPilot</div>', unsafe_allow_html=True)
    st.markdown(
        (
            '<div class="ap-subtitle">'
            f"{(district_context or {}).get('district_name') or 'District'}"
            " | Intelligent BPP rendition review with side-by-side document verification and batch triage."
            "</div>"
        ),
        unsafe_allow_html=True,
    )

    single_tab, batch_tab, queue_tab = st.tabs(["Single Review", "Batch Review", "Review Queue"])

    with single_tab:
        render_single_review()

    with batch_tab:
        render_batch_review()

    with queue_tab:
        render_review_queue()


if __name__ == "__main__":
    main()
