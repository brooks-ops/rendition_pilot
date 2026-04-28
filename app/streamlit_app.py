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
from app.pipeline import run_rendition_pipeline
from app.review_workflow import (
    APPRAISER_UPLOAD_DIR,
    COMPLETED_DIR,
    OUTPUT_DIR,
    QUEUE_CSV,
    append_queue_row,
    build_final_review_record,
    ensure_output_dirs,
    get_recommended_value,
    save_review_outputs,
    stamp_reviewed_pdf,
)


AUTHORIZED_USERS = {
    "bbarrett@lubbockcad.org",
    "bgarnica@lubbockcad.org",
    "emontoya@lubbockcad.org",
    "ctrimble@lubbockcad.org",
    "lflores@lubbockcad.org",
}

DEFAULT_SUPABASE_URL = "https://pzawjgckzcgnfsfuylqy.supabase.co"
DEFAULT_SUPABASE_ANON_KEY = "sb_publishable_q6lNn59Y-kz8lG0cYfJkYw_lL7xElsA"


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
    if st.session_state.get("authenticated_user") and st.session_state.get("supabase_access_token"):
        return

    access_token = st.query_params.get("session_token", "")
    if not access_token:
        return

    try:
        user = get_supabase_user(access_token)
    except Exception:
        st.query_params.clear()
        return

    email = str(user.get("email", "")).lower()
    if email in AUTHORIZED_USERS:
        st.session_state["authenticated_user"] = email
        st.session_state["supabase_access_token"] = access_token
    else:
        st.query_params.clear()


def persist_login(email: str, access_token: str) -> None:
    st.session_state["authenticated_user"] = email
    st.session_state["supabase_access_token"] = access_token
    st.query_params["session_token"] = access_token


def clear_login() -> None:
    st.session_state.pop("authenticated_user", None)
    st.session_state.pop("supabase_access_token", None)
    st.query_params.clear()


def require_login() -> bool:
    restore_login_from_query_params()

    if st.session_state.get("authenticated_user") in AUTHORIZED_USERS and st.session_state.get("supabase_access_token"):
        with st.sidebar:
            st.caption(f"Signed in as {st.session_state['authenticated_user']}")
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

    login_tab, create_tab = st.tabs(["Login", "Create Login"])

    with login_tab:
        with st.form("login_form"):
            email = st.text_input("Email", value="", placeholder="name@lubbockcad.org", key="login_email").strip().lower()
            password = st.text_input("Password", value="", type="password", key="login_password")
            submitted = st.form_submit_button("Login")

        if submitted:
            if email not in AUTHORIZED_USERS:
                st.error("This email is not authorized for AppraisalPilot.")
                return False

            try:
                auth_result = sign_in_with_supabase(email, password)
            except Exception as exc:
                st.error(f"Login failed: {exc}")
                return False

            access_token = auth_result.get("access_token")
            if not access_token:
                st.error("Login did not return a session. Confirm the account email first, then try again.")
                return False

            persist_login(email, access_token)
            st.rerun()

    with create_tab:
        with st.form("create_login_form"):
            new_email = st.text_input("Email", value="", placeholder="name@lubbockcad.org", key="signup_email").strip().lower()
            new_password = st.text_input("Password", value="", type="password", key="signup_password")
            confirm_password = st.text_input("Confirm Password", value="", type="password", key="signup_confirm_password")
            create_submitted = st.form_submit_button("Create Login")

        if create_submitted:
            if new_email not in AUTHORIZED_USERS:
                st.error("This email is not authorized to create an AppraisalPilot login.")
                return False
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

            if signup_result.get("session") or signup_result.get("access_token"):
                access_token = (
                    signup_result.get("access_token")
                    or (signup_result.get("session") or {}).get("access_token")
                )
                persist_login(new_email, access_token)
                st.rerun()
            else:
                st.success("Login created. Check your email if Supabase requires confirmation, then return to the Login tab.")

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


def prettify_path(path: str | None) -> str:
    mapping = {
        "use_manual_attachment_total": "Manual Attachment Total",
        "use_manual_good_faith_value": "Manual Good Faith Value",
        "use_manual_historical_cost_depreciated": "Historical Cost Less Depreciation",
        "use_schedule_rule_engine": "Schedule Rule Engine",
        "use_attachment_total_pending_review": "Attachment Total",
        "use_schedule_total_pending_review": "Schedule E Total",
        "use_good_faith_value_pending_review": "Good Faith Estimate",
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

    st.markdown(
        f"""
        <div class="ap-decision-card">
            <div class="ap-decision-label">Recommended Value</div>
            <div class="ap-decision-value">{format_money(value)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

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
    manual_override = result.get("manual_override", {}) or {}
    assessment = result.get("assessment_summary", {}) or {}
    depreciation = result.get("depreciated_override_result", {}) or {}
    schedule_breakdown = result.get("schedule_breakdown", {}) or {}
    schedule_e_breakdown = result.get("schedule_e_breakdown", {}) or {}
    valuation_flags = result.get("valuation_flags", []) or []

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
        st.markdown("<br>", unsafe_allow_html=True)
        render_kv_section(
            "Rule Engine Breakdown",
            [
                ("Schedule A", format_money(schedule_breakdown.get("schedule_a_total"))),
                ("Schedule B", format_money(schedule_breakdown.get("schedule_b_total"))),
                ("Schedule C", format_money(schedule_breakdown.get("schedule_c_total"))),
                ("Schedule D", format_money(schedule_breakdown.get("schedule_d_total"))),
                ("Schedule E", format_money(schedule_breakdown.get("schedule_e_total"))),
                ("E Furniture & Fixtures", format_money(schedule_e_breakdown.get("furniture_fixtures"))),
                ("E Machinery & Equipment", format_money(schedule_e_breakdown.get("machinery_equipment"))),
                ("E Office Equipment", format_money(schedule_e_breakdown.get("office_equipment"))),
                ("E Computer Equipment", format_money(schedule_e_breakdown.get("computer_equipment"))),
                ("E POS / Servers / Mainframes", format_money(schedule_e_breakdown.get("pos_servers_mainframes"))),
                ("E Other", format_money(schedule_e_breakdown.get("other"))),
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
                ("Valuation Flags", format_text(valuation_flags)),
                ("Issues", format_text(assessment.get("issues"))),
            ],
        )
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


def show_extracted_line_items(result: dict) -> None:
    line_items = result.get("extracted_line_items", []) or []

    st.markdown('<div class="ap-card">', unsafe_allow_html=True)
    st.subheader("Extracted Line Items")
    st.caption("Rows below show the schedule rule inputs, chosen values, and row-level flags.")

    if not line_items:
        st.info("No line items were extracted.")
        st.markdown("</div>", unsafe_allow_html=True)
        return

    rows = []
    for item in line_items:
        rows.append(
            {
                "Schedule": item.get("schedule"),
                "Subsection": item.get("subsection"),
                "Page": item.get("source_page"),
                "Year Acquired": item.get("year_acquired"),
                "Historical Cost": format_money(item.get("historical_cost")),
                "Good Faith": format_money(item.get("good_faith_value")),
                "Exact Value": format_money(item.get("exact_value")),
                "Calculated Value": format_money(item.get("calculated_value")),
                "Factor": format_percent(item.get("depreciation_factor")),
                "Source": item.get("value_source"),
                "Confidence": item.get("confidence"),
                "Flags": format_text(item.get("flags")),
                "Raw Text": item.get("raw_text"),
            }
        )

    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
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

    st.caption(f"{len(page_images)} page(s) rendered")

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
            if st.button("Rotate Left", key="single_pdf_rotate_left_single", use_container_width=True):
                st.session_state[rotation_key] = (current_rotation - 90) % 360
                st.rerun()
        with rotate_cols[1]:
            if st.button("Rotate Right", key="single_pdf_rotate_right_single", use_container_width=True):
                st.session_state[rotation_key] = (current_rotation + 90) % 360
                st.rerun()
        with rotate_cols[2]:
            st.caption(f"Rotation: {int(st.session_state.get(rotation_key, 0) or 0)}°")
        st.image(_rotate_png_bytes(page_images[0], int(st.session_state.get(rotation_key, 0) or 0)), use_container_width=True)
        return

    current_page = int(st.session_state.get(page_key, 1) or 1)
    current_rotation = int(st.session_state.get(rotation_key, 0) or 0)

    nav_cols = st.columns([1, 1, 1.25, 1, 1, 2.5])
    with nav_cols[0]:
        if st.button("Previous", key="single_pdf_prev_page", use_container_width=True, disabled=current_page <= 1):
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
        if st.button("Rotate Left", key="single_pdf_rotate_left", use_container_width=True):
            st.session_state[rotation_key] = (current_rotation - 90) % 360
            st.rerun()
    with nav_cols[4]:
        if st.button("Rotate Right", key="single_pdf_rotate_right", use_container_width=True):
            st.session_state[rotation_key] = (current_rotation + 90) % 360
            st.rerun()
    with nav_cols[5]:
        st.caption(f"Page {int(selected_page)} of {len(page_images)} | Rotation: {int(st.session_state.get(rotation_key, 0) or 0)}°")

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
def reset_single_review_state() -> None:
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


def finalize_review_panel(file_name: str, result: dict, file_bytes: bytes) -> None:
    recommended_value = get_recommended_value(result)
    default_source = (result.get("assessment_summary", {}) or {}).get("value_source") or "pipeline_recommendation"
    metadata = result.get("metadata", {}) or {}

    st.markdown('<div class="ap-card">', unsafe_allow_html=True)
    st.subheader("Finalize Review")
    st.caption("Confirm the final value, enter initials/account, then lock and save the reviewed rendition.")

    c1, c2 = st.columns([1, 1])
    with c1:
        final_value_text = st.text_input(
            "Final Value",
            value="" if recommended_value is None else str(recommended_value),
            key=f"final_value_{file_name}",
        )
    with c2:
        final_source = st.selectbox(
            "Final Source",
            list(dict.fromkeys([
                default_source,
                "manual_override",
                "attachment_total",
                "good_faith_value",
                "historical_cost_depreciated",
                "schedule_e_total",
                "agent_review",
                "manual_review",
            ])),
            key=f"final_source_{file_name}",
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
            ["Accepted Recommended Value", "Adjusted Value"],
            horizontal=True,
            key=f"final_decision_{file_name}",
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
        st.success(
            f"Locked {format_money(locked_record.get('final_value'))} "
            f"for {locked_record.get('account_number') or account_number}."
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
            final_value = parse_money_input(final_value_text)
            if final_value is None:
                st.error("Enter a valid final value before locking.")
            elif not appraiser_initials.strip():
                st.error("Enter appraiser initials before locking.")
            elif not account_number.strip():
                st.error("Enter the appraisal district account / P# before locking.")
            else:
                decision_code = "accepted" if decision == "Accepted Recommended Value" else "adjusted"
                record = build_final_review_record(
                    file_name=file_name,
                    result=result,
                    final_value=final_value,
                    final_source=final_source,
                    appraiser_notes=final_notes,
                    appraiser_initials=appraiser_initials.strip().upper(),
                    account_number=account_number.strip().upper(),
                    decision=decision_code,
                )
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
                    )
                    locked_record["stamped_pdf"] = str(stamped_pdf)
                    paths = save_review_outputs(file_name=file_name, result=result, final_record=locked_record)
                    append_queue_row(
                        file_name=file_name,
                        result={**result, "final_review": locked_record},
                        status="Locked",
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

    c1, c2 = st.columns([1.2, 1])

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

    notes = st.text_area("Notes", value="", key="single_notes", height=90)
    run_review = st.button("Run Review", type="primary", use_container_width=False, key="single_run_review")
    st.markdown("</div>", unsafe_allow_html=True)

    if not uploaded_file:
        st.info("Upload a rendition PDF to begin.")
        return

    file_bytes = uploaded_file.getvalue()

    left_col, right_col = st.columns([1.02, 1])

    with left_col:
        st.markdown('<div class="ap-card">', unsafe_allow_html=True)
        st.subheader("Rendition PDF")
        st.download_button(
            "Download PDF",
            data=file_bytes,
            file_name=uploaded_file.name,
            mime="application/pdf",
            use_container_width=True,
            key="single_download_pdf",
        )
        show_pdf_preview(file_bytes)
        st.markdown("</div>", unsafe_allow_html=True)

    with right_col:
        st.markdown('<div class="ap-card">', unsafe_allow_html=True)
        st.subheader("Analysis")
        st.markdown(
            '<div class="ap-muted">Focus on Recommended Value, Valuation Path, Confidence, and Reason first.</div>',
            unsafe_allow_html=True,
        )

        if run_review:
            manual_override = build_manual_override(
                mode=mode,
                attachment_total=attachment_total,
                good_faith_value=good_faith_value,
                historical_cost=historical_cost,
                acquisition_year=acquisition_year,
                life_years=life_years,
                notes=notes,
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

        if result:
            ocr_errors = (result.get("review_flags", {}) or {}).get("ocr_errors", []) or []
            if ocr_errors:
                for ocr_error in ocr_errors:
                    st.error(f"OCR provider error: {ocr_error}")

            show_top_metrics(result)
            render_manual_assist_panel(result_file_name, result, result_file_bytes)
            finalize_review_panel(result_file_name, result, result_file_bytes)

            with st.expander("Document / Form / Schedule Details", expanded=False):
                show_flags_and_findings(result)

            with st.expander("Extracted Line Items", expanded=False):
                show_extracted_line_items(result)

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
        else:
            st.info("Set inputs above, then click Run Review.")
        st.markdown("</div>", unsafe_allow_html=True)


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
            save_review_outputs(file_name=uploaded_file.name, result=result)
            append_queue_row(file_name=uploaded_file.name, result=result, status=get_status_label(result))
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
    ensure_output_dirs()
    st.markdown('<div class="ap-card">', unsafe_allow_html=True)
    st.subheader("Review Queue")
    st.caption(f"Saved outputs are written to {OUTPUT_DIR}")

    if QUEUE_CSV.exists():
        try:
            df = pd.read_csv(QUEUE_CSV)
            st.dataframe(df.sort_values(by="processed_at", ascending=False), use_container_width=True, hide_index=True)
            st.download_button(
                "Download Queue CSV",
                data=QUEUE_CSV.read_bytes(),
                file_name="review_queue.csv",
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
    completed_files = sorted(COMPLETED_DIR.glob("*_final.json"), key=lambda path: path.stat().st_mtime, reverse=True)
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

    st.markdown('<div class="ap-title">AppraisalPilot</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="ap-subtitle">Intelligent BPP rendition review with side-by-side document verification and batch triage.</div>',
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
