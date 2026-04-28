from __future__ import annotations

import base64
import os
import re
from pathlib import Path
from typing import Any

import requests

from app.extractor import PDFExtractor
from app.rendition_value_engine import (
    RenditionLineItem,
    calculate_schedule_a,
    calculate_schedule_b,
    calculate_schedule_c,
    calculate_schedule_d,
    calculate_schedule_e,
)
SCHEDULE_HEADINGS = ["A", "B", "C", "D", "E"]
REQUIRED_COLUMN_MARKERS = [
    "good faith",
    "historical cost",
    "year acquired",
    "inventory",
    "actual cost",
    "supplies",
]

SCHEDULE_E_SCHEMA_KEYS = {
    "furniture_fixtures": "furniture_fixtures_items",
    "machinery_equipment": "machinery_equipment_items",
    "computer_equipment": "computers_items",
    "office_equipment": "other_items",
    "pos_servers_mainframes": "other_items",
    "other": "other_items",
}


def build_empty_rendition_schema() -> dict[str, Any]:
    return {
        "schedule_a": {
            "good_faith_values": [],
            "historical_cost_items": [],
            "raw_rows": [],
            "confidence": 0.0,
        },
        "schedule_b": {
            "inventory_values": [],
            "raw_rows": [],
            "confidence": 0.0,
        },
        "schedule_c": {
            "supplies_values": [],
            "raw_rows": [],
            "confidence": 0.0,
        },
        "schedule_d": {
            "good_faith_values": [],
            "historical_cost_items": [],
            "raw_rows": [],
            "confidence": 0.0,
        },
        "schedule_e": {
            "furniture_fixtures_items": [],
            "machinery_equipment_items": [],
            "computers_items": [],
            "other_items": [],
            "raw_rows": [],
            "confidence": 0.0,
        },
        "document_confidence": 0.0,
        "review_flags": [],
    }


def get_document_ai_env_status() -> dict[str, bool]:
    processor_name = bool(_get_env("GOOGLE_DOCUMENT_AI_PROCESSOR_NAME"))
    processor_parts = all(
        bool(_get_env(name))
        for name in [
            "GOOGLE_DOCUMENT_AI_PROJECT_ID",
            "GOOGLE_DOCUMENT_AI_LOCATION",
            "GOOGLE_DOCUMENT_AI_PROCESSOR_ID",
        ]
    )
    auth_available = bool(
        _get_env("GOOGLE_APPLICATION_CREDENTIALS")
        or _get_env("GOOGLE_DOCUMENT_AI_ACCESS_TOKEN")
        or _get_env("GOOGLE_DOCUMENT_AI_API_KEY")
    )
    return {
        "processor_name_present": processor_name,
        "processor_parts_present": processor_parts,
        "credentials_present": bool(_get_env("GOOGLE_APPLICATION_CREDENTIALS")),
        "access_token_present": bool(_get_env("GOOGLE_DOCUMENT_AI_ACCESS_TOKEN")),
        "api_key_present": bool(_get_env("GOOGLE_DOCUMENT_AI_API_KEY")),
        "auth_available": auth_available,
    }


def extract_pdf_text(file: str | os.PathLike[str]) -> dict[str, Any]:
    extractor = PDFExtractor()
    pages = extractor.extract_pages(str(file))
    combined_text = "\n\n".join((page.get("text") or "").strip() for page in pages if page.get("text"))
    quality = assess_extracted_text_quality(combined_text, pages=pages)
    return {
        "text": combined_text,
        "pages": pages,
        "quality_score": quality["score"],
        "usable": quality["usable"],
        "quality_details": quality,
    }


def should_use_document_ai(extracted_text: str, extraction_quality: dict[str, Any] | float | bool | None) -> bool:
    if isinstance(extraction_quality, dict):
        score = float(extraction_quality.get("score") or 0.0)
        usable = bool(extraction_quality.get("usable"))
        missing_schedule_count = len(extraction_quality.get("missing_schedules") or [])
        missing_columns = bool(extraction_quality.get("missing_columns"))
        unreadable_tables = bool(extraction_quality.get("table_columns_unreadable"))
    else:
        score = float(extraction_quality or 0.0) if extraction_quality not in {True, False, None} else (1.0 if extraction_quality is True else 0.0)
        usable = bool(extraction_quality)
        missing_schedule_count = 0
        missing_columns = False
        unreadable_tables = False

    text = str(extracted_text or "").strip()
    if not text:
        return True
    if len(text) < 120:
        return True
    if len(text) < 250 and (not usable or score < 0.8 or missing_schedule_count > 0 or missing_columns or unreadable_tables):
        return True
    if not usable or score < 0.55:
        return True
    if missing_schedule_count >= 2:
        return True
    if missing_columns or unreadable_tables:
        return True
    return False


def run_google_document_ai(file: str | os.PathLike[str]) -> dict[str, Any]:
    processor_name = get_google_document_ai_processor_name()
    if not processor_name:
        raise RuntimeError("Google Document AI processor is not configured.")

    location_match = re.search(r"/locations/([^/]+)/processors/", processor_name)
    location = location_match.group(1) if location_match else (_get_env("GOOGLE_DOCUMENT_AI_LOCATION") or "us")
    endpoint = f"https://{location}-documentai.googleapis.com/v1/{processor_name}:process"
    headers, params = _build_document_ai_auth()

    pdf_bytes = Path(file).read_bytes()
    payload = {
        "skipHumanReview": True,
        "rawDocument": {
            "content": base64.b64encode(pdf_bytes).decode("ascii"),
            "mimeType": "application/pdf",
        },
        "processOptions": {
            "ocrConfig": {
                "enableNativePdfParsing": True,
                "enableImageQualityScores": True,
                "enableSymbol": False,
            }
        },
    }

    response = requests.post(endpoint, headers=headers, params=params, json=payload, timeout=30)
    if response.status_code >= 400:
        raise RuntimeError(f"Google Document AI HTTP {response.status_code}: {response.text[:500]}")

    try:
        result = response.json()
    except ValueError as exc:
        raise RuntimeError(f"Google Document AI returned non-JSON response: {type(exc).__name__}: {exc}") from exc

    document = result.get("document") or result
    return {
        "text": document.get("text") or "",
        "pages": document.get("pages") or [],
        "tables": _collect_document_ai_tables(document),
        "form_fields": _collect_document_ai_form_fields(document),
        "confidence": _document_ai_confidence(document),
        "layout": _collect_document_ai_layout(document),
        "document": document,
        "raw_result": result,
    }


def parse_document_ai_to_rendition_schema(document_ai_result: dict[str, Any]) -> dict[str, Any]:
    schema = build_empty_rendition_schema()
    document = document_ai_result.get("document") or document_ai_result
    pages = document.get("pages") or document_ai_result.get("pages") or []
    document_text = str(document.get("text") or document_ai_result.get("text") or "")
    converted_pages = _document_ai_pages_to_internal_pages(document_text, pages)
    line_items = _extract_line_items_from_pages(converted_pages)

    _populate_schema_from_line_items(schema, line_items)
    _attach_document_ai_raw_rows(schema, document_ai_result)

    schema["document_confidence"] = _document_ai_confidence(document)
    _finalize_schema_confidence(schema)
    _append_schema_review_flags(schema)
    return schema


def parse_text_to_rendition_schema(extracted_text: str, pages: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    schema = build_empty_rendition_schema()
    internal_pages = pages or [{"page_number": 1, "text": extracted_text or "", "ocr_blocks": []}]
    line_items = _extract_line_items_from_pages(internal_pages)
    _populate_schema_from_line_items(schema, line_items)
    quality = assess_extracted_text_quality(extracted_text, pages=internal_pages)
    schema["document_confidence"] = float(quality["score"])
    _finalize_schema_confidence(schema)
    _append_schema_review_flags(schema)
    return schema


def apply_rendition_valuation_rules(schema: dict[str, Any]) -> dict[str, Any]:
    schedule_a_items = _schema_schedule_to_line_items(schema, "A")
    schedule_b_items = _schema_schedule_to_line_items(schema, "B")
    schedule_c_items = _schema_schedule_to_line_items(schema, "C")
    schedule_d_items = _schema_schedule_to_line_items(schema, "D")
    schedule_e_items = _schema_schedule_to_line_items(schema, "E")

    schedule_a = calculate_schedule_a(schedule_a_items)
    schedule_b = calculate_schedule_b(schedule_b_items)
    schedule_c = calculate_schedule_c(schedule_c_items)
    schedule_d = calculate_schedule_d(schedule_d_items)
    schedule_e = calculate_schedule_e(schedule_e_items)

    review_flags = sorted(
        set(
            list(schema.get("review_flags") or [])
            + list(schedule_a.get("flags") or [])
            + list(schedule_b.get("flags") or [])
            + list(schedule_c.get("flags") or [])
            + list(schedule_d.get("flags") or [])
            + list(schedule_e.get("flags") or [])
        )
    )
    schedule_breakdown = {
        "schedule_a": {
            "total": round(float(schedule_a.get("total") or 0.0), 2),
            "confidence": float((schema.get("schedule_a") or {}).get("confidence") or 0.0),
            "raw_rows": (schema.get("schedule_a") or {}).get("raw_rows") or [],
        },
        "schedule_b": {
            "total": round(float(schedule_b.get("total") or 0.0), 2),
            "confidence": float((schema.get("schedule_b") or {}).get("confidence") or 0.0),
            "raw_rows": (schema.get("schedule_b") or {}).get("raw_rows") or [],
        },
        "schedule_c": {
            "total": round(float(schedule_c.get("total") or 0.0), 2),
            "confidence": float((schema.get("schedule_c") or {}).get("confidence") or 0.0),
            "raw_rows": (schema.get("schedule_c") or {}).get("raw_rows") or [],
        },
        "schedule_d": {
            "total": round(float(schedule_d.get("total") or 0.0), 2),
            "confidence": float((schema.get("schedule_d") or {}).get("confidence") or 0.0),
            "raw_rows": (schema.get("schedule_d") or {}).get("raw_rows") or [],
        },
        "schedule_e": {
            "total": round(float(schedule_e.get("total") or 0.0), 2),
            "confidence": float((schema.get("schedule_e") or {}).get("confidence") or 0.0),
            "raw_rows": (schema.get("schedule_e") or {}).get("raw_rows") or [],
            "categories": {
                "furniture_fixtures": round(float((schedule_e.get("subsection_totals") or {}).get("furniture_fixtures") or 0.0), 2),
                "machinery_equipment": round(float((schedule_e.get("subsection_totals") or {}).get("machinery_equipment") or 0.0), 2),
                "office_equipment": round(float((schedule_e.get("subsection_totals") or {}).get("office_equipment") or 0.0), 2),
                "computer_equipment": round(float((schedule_e.get("subsection_totals") or {}).get("computer_equipment") or 0.0), 2),
                "pos_servers_mainframes": round(float((schedule_e.get("subsection_totals") or {}).get("pos_servers_mainframes") or 0.0), 2),
                "other": round(float((schedule_e.get("subsection_totals") or {}).get("other") or 0.0), 2),
            },
        },
    }
    recommended_value = round(
        sum(section["total"] for section in schedule_breakdown.values() if isinstance(section, dict) and "total" in section),
        2,
    )
    confidence = _numeric_confidence_to_label(
        min(
            1.0,
            max(
                0.0,
                (
                    float(schema.get("document_confidence") or 0.0)
                    + _confidence_average(
                        [
                            (schema.get("schedule_a") or {}).get("confidence"),
                            (schema.get("schedule_b") or {}).get("confidence"),
                            (schema.get("schedule_c") or {}).get("confidence"),
                            (schema.get("schedule_d") or {}).get("confidence"),
                            (schema.get("schedule_e") or {}).get("confidence"),
                        ]
                    )
                )
                / 2.0,
            ),
        )
    )
    return {
        "recommended_value": recommended_value,
        "schedule_breakdown": schedule_breakdown,
        "confidence": confidence,
        "review_flags": review_flags,
        "line_items": [
            item.to_dict()
            for item in schedule_a_items + schedule_b_items + schedule_c_items + schedule_d_items + schedule_e_items
        ],
        "valuation_flags": review_flags,
    }


def process_uploaded_rendition(file: str | os.PathLike[str]) -> dict[str, Any]:
    extraction_provider = "embedded_text"
    document_ai_used = False
    document_ai_error = None

    extraction = extract_pdf_text(file)
    extracted_text = extraction["text"]
    quality_details = extraction["quality_details"]
    missing_schedules = quality_details.get("missing_schedules") or []

    schema: dict[str, Any]
    if should_use_document_ai(extracted_text, quality_details):
        try:
            document_ai_result = run_google_document_ai(file)
            schema = parse_document_ai_to_rendition_schema(document_ai_result)
            extraction_provider = "google_document_ai"
            document_ai_used = True
        except Exception as exc:
            document_ai_error = f"{type(exc).__name__}: {exc}"
            schema = parse_text_to_rendition_schema(extracted_text, pages=extraction.get("pages") or [])
            extraction_provider = "fallback_text"
            schema["review_flags"] = sorted(set(list(schema.get("review_flags") or []) + ["document_ai_failed_fallback_used"]))
    else:
        schema = parse_text_to_rendition_schema(extracted_text, pages=extraction.get("pages") or [])

    valuation = apply_rendition_valuation_rules(schema)
    low_confidence_sections = [
        key
        for key in ["schedule_a", "schedule_b", "schedule_c", "schedule_d", "schedule_e"]
        if float((schema.get(key) or {}).get("confidence") or 0.0) < 0.55
    ]
    result = {
        "recommended_value": valuation["recommended_value"],
        "extraction_provider": extraction_provider,
        "document_confidence": float(schema.get("document_confidence") or 0.0),
        "schedule_breakdown": valuation["schedule_breakdown"],
        "normalized_schema": schema,
        "review_flags": valuation["review_flags"],
        "confidence": valuation["confidence"],
        "line_items": valuation["line_items"],
        "debug": {
            "text_quality_score": float(extraction.get("quality_score") or 0.0),
            "document_ai_used": document_ai_used,
            "document_ai_error": document_ai_error,
            "missing_schedules": missing_schedules,
            "low_confidence_sections": low_confidence_sections,
            "document_ai_env": get_document_ai_env_status(),
        },
    }
    return result


def assess_extracted_text_quality(text: str, pages: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    page_count = max(1, len(pages or []))
    length_score = min(1.0, len(normalized) / float(700 * page_count))
    present_schedules = [
        letter for letter in SCHEDULE_HEADINGS if f"schedule {letter.lower()}" in normalized
    ]
    schedule_score = len(present_schedules) / float(len(SCHEDULE_HEADINGS))
    found_columns = [marker for marker in REQUIRED_COLUMN_MARKERS if marker in normalized]
    column_score = len(found_columns) / float(len(REQUIRED_COLUMN_MARKERS))
    alpha_ratio = (
        sum(1 for ch in normalized if ch.isalpha()) / max(1, len(normalized))
        if normalized
        else 0.0
    )
    table_columns_unreadable = column_score < 0.34
    score = round((length_score * 0.35) + (schedule_score * 0.35) + (column_score * 0.2) + (alpha_ratio * 0.1), 3)
    usable = bool(normalized) and score >= 0.55 and len(present_schedules) >= 2
    return {
        "score": score,
        "usable": usable,
        "missing_schedules": [letter for letter in SCHEDULE_HEADINGS if letter not in present_schedules],
        "missing_columns": [marker for marker in REQUIRED_COLUMN_MARKERS if marker not in found_columns],
        "table_columns_unreadable": table_columns_unreadable,
    }


def get_google_document_ai_processor_name() -> str | None:
    processor_name = _get_env("GOOGLE_DOCUMENT_AI_PROCESSOR_NAME")
    if processor_name:
        return processor_name.strip().lstrip("/")
    project_id = _get_env("GOOGLE_DOCUMENT_AI_PROJECT_ID") or _get_env("GOOGLE_CLOUD_PROJECT")
    location = _get_env("GOOGLE_DOCUMENT_AI_LOCATION")
    processor_id = _get_env("GOOGLE_DOCUMENT_AI_PROCESSOR_ID")
    if project_id and location and processor_id:
        return f"projects/{project_id}/locations/{location}/processors/{processor_id}"
    return None


def _build_document_ai_auth() -> tuple[dict[str, str], dict[str, str]]:
    headers = {"Content-Type": "application/json; charset=utf-8"}
    params: dict[str, str] = {}
    access_token = _get_env("GOOGLE_DOCUMENT_AI_ACCESS_TOKEN")
    api_key = _get_env("GOOGLE_DOCUMENT_AI_API_KEY")
    if api_key:
        params["key"] = api_key
        return headers, params
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
        return headers, params

    token = _load_google_auth_access_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
        return headers, params
    raise RuntimeError("No Google Document AI authentication method is available.")


def _load_google_auth_access_token() -> str | None:
    try:
        from google.auth.transport.requests import Request
        import google.auth
    except Exception:
        return None

    try:
        credentials, _project = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        credentials.refresh(Request())
    except Exception:
        return None
    return getattr(credentials, "token", None)


def _get_env(*names: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return ""


def _document_ai_pages_to_internal_pages(document_text: str, raw_pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    for raw_page in raw_pages or []:
        lines = raw_page.get("lines") or []
        tokens = raw_page.get("tokens") or []
        page_text_parts = []
        for line in lines:
            page_text_parts.append(_document_ai_anchor_text(document_text, (line.get("layout") or {}).get("textAnchor") or {}))
        word_blocks = []
        dimension = raw_page.get("dimension") or {}
        width = float(dimension.get("width") or 0)
        height = float(dimension.get("height") or 0)
        for token in tokens:
            layout = token.get("layout") or {}
            token_text = _document_ai_anchor_text(document_text, layout.get("textAnchor") or {})
            token_text = str(token_text or "").strip()
            if not token_text:
                continue
            xs, ys = _document_ai_poly_xy(layout.get("boundingPoly") or {}, width, height)
            word_blocks.append(
                {
                    "text": token_text,
                    "x0": min(xs) if xs else 0.0,
                    "x1": max(xs) if xs else 0.0,
                    "top": min(ys) if ys else 0.0,
                    "y0": min(ys) if ys else 0.0,
                    "y1": max(ys) if ys else 0.0,
                    "confidence": token.get("confidence", layout.get("confidence")),
                }
            )
        pages.append(
            {
                "page_number": int(raw_page.get("pageNumber") or len(pages) + 1),
                "text": "\n".join(part.strip() for part in page_text_parts if str(part or "").strip()),
                "ocr_blocks": word_blocks,
                "text_source": "google_document_ai",
            }
        )
    if not pages and document_text.strip():
        pages.append({"page_number": 1, "text": document_text.strip(), "ocr_blocks": [], "text_source": "google_document_ai"})
    return pages


def _document_ai_anchor_text(document_text: str, text_anchor: dict[str, Any]) -> str:
    segments = text_anchor.get("textSegments") or []
    parts: list[str] = []
    for segment in segments:
        start = int(segment.get("startIndex") or 0)
        end = int(segment.get("endIndex") or 0)
        if end > start:
            parts.append(document_text[start:end])
    return "".join(parts)


def _document_ai_poly_xy(bounding_poly: dict[str, Any], page_width: float, page_height: float) -> tuple[list[float], list[float]]:
    vertices = bounding_poly.get("vertices") or []
    if vertices:
        xs = [float(vertex.get("x", 0) or 0) for vertex in vertices]
        ys = [float(vertex.get("y", 0) or 0) for vertex in vertices]
        return xs, ys
    normalized = bounding_poly.get("normalizedVertices") or []
    xs = [float(vertex.get("x", 0) or 0) * page_width for vertex in normalized]
    ys = [float(vertex.get("y", 0) or 0) * page_height for vertex in normalized]
    return xs, ys


def _collect_document_ai_tables(document: dict[str, Any]) -> list[dict[str, Any]]:
    tables: list[dict[str, Any]] = []
    document_text = str(document.get("text") or "")
    for page in document.get("pages") or []:
        for table in page.get("tables") or []:
            rows = []
            for row in table.get("bodyRows") or []:
                cells = []
                for cell in row.get("cells") or []:
                    layout = cell.get("layout") or {}
                    cells.append(_document_ai_anchor_text(document_text, layout.get("textAnchor") or {}))
                rows.append(cells)
            tables.append(
                {
                    "page_number": page.get("pageNumber"),
                    "rows": rows,
                    "confidence": table.get("layout", {}).get("confidence"),
                }
            )
    return tables


def _collect_document_ai_form_fields(document: dict[str, Any]) -> list[dict[str, Any]]:
    form_fields: list[dict[str, Any]] = []
    document_text = str(document.get("text") or "")
    for page in document.get("pages") or []:
        for field in page.get("formFields") or []:
            field_name = _document_ai_anchor_text(document_text, (field.get("fieldName") or {}).get("textAnchor") or {})
            field_value = _document_ai_anchor_text(document_text, (field.get("fieldValue") or {}).get("textAnchor") or {})
            form_fields.append(
                {
                    "page_number": page.get("pageNumber"),
                    "name": field_name,
                    "value": field_value,
                    "confidence": field.get("valueType"),
                }
            )
    return form_fields


def _collect_document_ai_layout(document: dict[str, Any]) -> list[dict[str, Any]]:
    layouts = []
    for page in document.get("pages") or []:
        layouts.append(
            {
                "page_number": page.get("pageNumber"),
                "dimension": page.get("dimension"),
                "image_quality_scores": (page.get("imageQualityScores") or {}),
            }
        )
    return layouts


def _document_ai_confidence(document: dict[str, Any]) -> float:
    confidences: list[float] = []
    for page in document.get("pages") or []:
        image_scores = page.get("imageQualityScores") or {}
        quality_score = image_scores.get("qualityScore")
        if quality_score is not None:
            confidences.append(float(quality_score))
        for token in page.get("tokens") or []:
            confidence = token.get("confidence") or (token.get("layout") or {}).get("confidence")
            if confidence is not None:
                confidences.append(float(confidence))
    return round(_confidence_average(confidences), 3)


def _extract_line_items_from_pages(pages: list[dict[str, Any]]) -> list[RenditionLineItem]:
    from app.rendition_value_engine import extract_line_items

    return extract_line_items({"pages": pages})


def _populate_schema_from_line_items(schema: dict[str, Any], line_items: list[RenditionLineItem]) -> None:
    for item in line_items:
        confidence = float(item.confidence or 0.0)
        raw_row = {
            "description": item.raw_text,
            "raw_text": item.raw_text,
            "page_number": item.source_page,
            "confidence": confidence,
            "subsection": item.subsection,
            "year_acquired": item.year_acquired,
            "historical_cost": item.historical_cost,
            "good_faith_value": item.good_faith_value,
            "exact_value": item.exact_value,
            "raw_values": item.raw_values,
            "flags": item.flags,
        }
        schedule_key = f"schedule_{item.schedule.lower()}"
        if schedule_key not in schema:
            continue
        schema[schedule_key]["raw_rows"].append(raw_row)

        if item.schedule == "A":
            if item.good_faith_value is not None:
                schema["schedule_a"]["good_faith_values"].append(float(item.good_faith_value))
            if item.historical_cost is not None:
                schema["schedule_a"]["historical_cost_items"].append(
                    {
                        "description": item.raw_text,
                        "cost": float(item.historical_cost),
                        "year_acquired": item.year_acquired,
                        "confidence": confidence,
                        "page_number": item.source_page,
                    }
                )
        elif item.schedule == "B":
            exact_value = item.exact_value if item.exact_value is not None else item.good_faith_value
            if exact_value is not None:
                schema["schedule_b"]["inventory_values"].append(float(exact_value))
        elif item.schedule == "C":
            exact_value = item.exact_value if item.exact_value is not None else item.good_faith_value
            if exact_value is not None:
                schema["schedule_c"]["supplies_values"].append(float(exact_value))
        elif item.schedule == "D":
            if item.good_faith_value is not None:
                schema["schedule_d"]["good_faith_values"].append(float(item.good_faith_value))
            if item.historical_cost is not None:
                schema["schedule_d"]["historical_cost_items"].append(
                    {
                        "description": item.raw_text,
                        "cost": float(item.historical_cost),
                        "year_acquired": item.year_acquired,
                        "confidence": confidence,
                        "page_number": item.source_page,
                    }
                )
        elif item.schedule == "E":
            subsection_key = SCHEDULE_E_SCHEMA_KEYS.get(item.subsection or "", "other_items")
            schema["schedule_e"][subsection_key].append(
                {
                    "description": item.raw_text,
                    "subsection": item.subsection,
                    "historical_cost": item.historical_cost,
                    "good_faith_value": item.good_faith_value,
                    "year_acquired": item.year_acquired,
                    "confidence": confidence,
                    "page_number": item.source_page,
                    "raw_values": item.raw_values,
                }
            )


def _attach_document_ai_raw_rows(schema: dict[str, Any], document_ai_result: dict[str, Any]) -> None:
    tables = document_ai_result.get("tables") or []
    for table in tables:
        schedule_key = _guess_schedule_from_table(table)
        if schedule_key and schedule_key in schema:
            schema[schedule_key]["raw_rows"].append(
                {
                    "source": "document_ai_table",
                    "page_number": table.get("page_number"),
                    "confidence": table.get("confidence"),
                    "rows": table.get("rows") or [],
                }
            )


def _guess_schedule_from_table(table: dict[str, Any]) -> str | None:
    flattened = " ".join(" ".join(str(cell) for cell in row) for row in table.get("rows") or []).lower()
    for letter in SCHEDULE_HEADINGS:
        if f"schedule {letter.lower()}" in flattened:
            return f"schedule_{letter.lower()}"
    if "good faith" in flattened:
        return "schedule_a"
    if "inventory" in flattened:
        return "schedule_b"
    if "supplies" in flattened:
        return "schedule_c"
    return None


def _finalize_schema_confidence(schema: dict[str, Any]) -> None:
    for schedule_key in ["schedule_a", "schedule_b", "schedule_c", "schedule_d", "schedule_e"]:
        confidences = []
        for row in (schema.get(schedule_key) or {}).get("raw_rows") or []:
            confidence = row.get("confidence")
            if confidence is not None:
                confidences.append(float(confidence))
        schema[schedule_key]["confidence"] = round(_confidence_average(confidences), 3)


def _append_schema_review_flags(schema: dict[str, Any]) -> None:
    flags = set(schema.get("review_flags") or [])
    for schedule_key in ["schedule_a", "schedule_b", "schedule_c", "schedule_d", "schedule_e"]:
        confidence = float((schema.get(schedule_key) or {}).get("confidence") or 0.0)
        if confidence < 0.55:
            flags.add(f"low_confidence_{schedule_key}")
        if not (schema.get(schedule_key) or {}).get("raw_rows"):
            flags.add(f"missing_{schedule_key}")
    schema["review_flags"] = sorted(flags)


def _schema_schedule_to_line_items(schema: dict[str, Any], schedule: str) -> list[RenditionLineItem]:
    schedule_key = f"schedule_{schedule.lower()}"
    section = schema.get(schedule_key) or {}
    items: list[RenditionLineItem] = []
    if schedule == "A":
        for value in section.get("good_faith_values") or []:
            items.append(RenditionLineItem(schedule="A", good_faith_value=float(value), raw_text="Schedule A Good Faith"))
        for row in section.get("historical_cost_items") or []:
            items.append(
                RenditionLineItem(
                    schedule="A",
                    raw_text=str(row.get("description") or ""),
                    historical_cost=float(row.get("cost") or 0.0),
                    year_acquired=row.get("year_acquired"),
                    confidence=row.get("confidence"),
                    source_page=row.get("page_number"),
                )
            )
    elif schedule == "B":
        for value in section.get("inventory_values") or []:
            items.append(RenditionLineItem(schedule="B", exact_value=float(value), raw_text="Schedule B Inventory"))
    elif schedule == "C":
        for value in section.get("supplies_values") or []:
            items.append(RenditionLineItem(schedule="C", exact_value=float(value), raw_text="Schedule C Supplies"))
    elif schedule == "D":
        for value in section.get("good_faith_values") or []:
            items.append(RenditionLineItem(schedule="D", good_faith_value=float(value), raw_text="Schedule D Good Faith"))
        for row in section.get("historical_cost_items") or []:
            items.append(
                RenditionLineItem(
                    schedule="D",
                    raw_text=str(row.get("description") or ""),
                    historical_cost=float(row.get("cost") or 0.0),
                    year_acquired=row.get("year_acquired"),
                    confidence=row.get("confidence"),
                    source_page=row.get("page_number"),
                )
            )
    elif schedule == "E":
        for key in ["furniture_fixtures_items", "machinery_equipment_items", "computers_items", "other_items"]:
            for row in section.get(key) or []:
                subsection = row.get("subsection")
                if not subsection:
                    if key == "furniture_fixtures_items":
                        subsection = "furniture_fixtures"
                    elif key == "machinery_equipment_items":
                        subsection = "machinery_equipment"
                    elif key == "computers_items":
                        subsection = "computer_equipment"
                    else:
                        subsection = "other"
                items.append(
                    RenditionLineItem(
                        schedule="E",
                        subsection=subsection,
                        raw_text=str(row.get("description") or ""),
                        historical_cost=row.get("historical_cost"),
                        good_faith_value=row.get("good_faith_value"),
                        year_acquired=row.get("year_acquired"),
                        confidence=row.get("confidence"),
                        source_page=row.get("page_number"),
                        raw_values=dict(row.get("raw_values") or {}),
                    )
                )
    return items


def _confidence_average(values: list[Any]) -> float:
    numeric = [float(value) for value in values if value is not None]
    if not numeric:
        return 0.0
    return sum(numeric) / float(len(numeric))


def _numeric_confidence_to_label(value: float) -> str:
    if value >= 0.8:
        return "high"
    if value >= 0.55:
        return "medium"
    return "low"
