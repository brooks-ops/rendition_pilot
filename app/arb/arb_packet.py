from __future__ import annotations

import io
import re
import textwrap
from datetime import datetime
from typing import Any

from app.arb.arb_models import ARBCaseInfo


def build_updated_evidence_packet(
    *,
    cad_pdf_bytes: bytes,
    case_info: ARBCaseInfo,
    selected_sections: dict[str, str],
    rebuttal_argument: str,
    hearing_prep: list[str],
    copy_ready_rebuttal: str,
) -> tuple[str, bytes]:
    import fitz

    try:
        doc = fitz.open(stream=cad_pdf_bytes, filetype="pdf")
    except Exception as exc:
        raise ValueError(f"CAD evidence packet must be a PDF to append ARB notes: {exc}") from exc

    try:
        appendix_lines = _build_appendix_lines(
            case_info=case_info,
            selected_sections=selected_sections,
            rebuttal_argument=rebuttal_argument,
            hearing_prep=hearing_prep,
            copy_ready_rebuttal=copy_ready_rebuttal,
        )
        _append_text_pages(doc, appendix_lines)
        output = doc.tobytes(deflate=True, garbage=4)
    finally:
        doc.close()

    return _packet_file_name(case_info), output


def _build_appendix_lines(
    *,
    case_info: ARBCaseInfo,
    selected_sections: dict[str, str],
    rebuttal_argument: str,
    hearing_prep: list[str],
    copy_ready_rebuttal: str,
) -> list[str]:
    lines = [
        "ARB REBUTTAL AND HEARING PREP APPENDIX",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        f"Account Number: {case_info.account_number or '-'}",
        f"Property Owner: {case_info.property_owner or '-'}",
        f"Property Address: {case_info.property_address or '-'}",
        f"Property Type: {case_info.property_type or '-'}",
        f"Tax Year: {case_info.tax_year or '-'}",
        f"Current Noticed Value: {case_info.current_noticed_value or '-'}",
        f"CAD Proposed Value: {case_info.cad_proposed_value or '-'}",
        f"Agent Requested Value: {case_info.agent_requested_value or '-'}",
        "",
        "APPRAISER REVIEW NOTICE",
        "This appendix is a preparation aid. Final hearing position, rebuttal language, and value conclusion remain subject to appraiser review and approval.",
        "",
    ]

    if rebuttal_argument.strip():
        lines.extend(["BUILT REBUTTAL ARGUMENT", rebuttal_argument.strip(), ""])

    if selected_sections:
        lines.append("SELECTED REVIEW SECTIONS")
        for title, content in selected_sections.items():
            if not str(content or "").strip():
                continue
            lines.extend(["", str(title).upper(), str(content).strip()])
        lines.append("")

    if hearing_prep:
        lines.append("ARB HEARING PREP")
        lines.extend(_bullet_lines(hearing_prep))
        lines.append("")

    if copy_ready_rebuttal.strip():
        lines.extend(["COPY-READY REBUTTAL NOTES", copy_ready_rebuttal.strip(), ""])

    return lines


def _append_text_pages(doc: Any, lines: list[str]) -> None:
    import fitz

    page_width = 612
    page_height = 792
    margin = 54
    font_size = 10
    leading = 14
    max_chars = 92
    y_start = 56
    y_limit = page_height - 54
    page = None
    y = y_start

    def new_page() -> Any:
        created = doc.new_page(width=page_width, height=page_height)
        created.insert_text(
            fitz.Point(margin, 30),
            "ARB Pilot Appendix",
            fontsize=9,
            fontname="helv",
            color=(0.25, 0.25, 0.25),
        )
        return created

    page = new_page()
    for raw_line in lines:
        wrapped = _wrap_line(raw_line, max_chars)
        for line in wrapped:
            if y > y_limit:
                page = new_page()
                y = y_start
            is_heading = bool(line.strip()) and line.strip() == line.strip().upper() and len(line.strip()) <= 70
            page.insert_text(
                fitz.Point(margin, y),
                line,
                fontsize=12 if is_heading else font_size,
                fontname="helv",
                color=(0, 0, 0),
            )
            y += leading + (4 if is_heading else 0)


def _wrap_line(line: str, max_chars: int) -> list[str]:
    if not line:
        return [""]
    normalized = re.sub(r"\s+", " ", str(line)).strip()
    if not normalized:
        return [""]
    prefix = ""
    if normalized.startswith("- "):
        prefix = "- "
        normalized = normalized[2:].strip()
    wrapped = textwrap.wrap(normalized, width=max_chars - len(prefix)) or [""]
    if not prefix:
        return wrapped
    return [f"{prefix}{wrapped[0]}", *[f"  {item}" for item in wrapped[1:]]]


def _bullet_lines(items: list[str]) -> list[str]:
    return [f"- {item}" for item in items if str(item or "").strip()]


def _packet_file_name(case_info: ARBCaseInfo) -> str:
    account = str(case_info.account_number or "").strip().upper()
    account = account or "ARB_PACKET"
    safe = re.sub(r"[^A-Z0-9#_-]+", "_", account).strip("_")
    return f"{safe or 'ARB_PACKET'}.pdf"

