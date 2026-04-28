from __future__ import annotations

import csv
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "Output"
COMPLETED_DIR = OUTPUT_DIR / "completed_reviews"
APPRAISER_UPLOAD_DIR = OUTPUT_DIR / "appraiser_uploads"
QUEUE_CSV = OUTPUT_DIR / "review_queue.csv"


def ensure_output_dirs() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    COMPLETED_DIR.mkdir(parents=True, exist_ok=True)
    APPRAISER_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def safe_stem(file_name: str) -> str:
    stem = Path(file_name).stem or "rendition"
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", stem).strip("._")
    return stem or "rendition"


def safe_account_number(value: Any) -> str:
    if value is None:
        return ""
    cleaned = re.sub(r"[^A-Za-z0-9]", "", str(value)).upper()
    return cleaned if re.fullmatch(r"P\d{4,10}", cleaned) else cleaned


def get_recommended_value(result: dict[str, Any]) -> Any:
    assessment = result.get("assessment_summary", {}) or {}
    return (
        assessment.get("recommended_value")
        or assessment.get("recommended_market_value")
        or assessment.get("recommended_assessed_value")
        or assessment.get("extracted_value")
    )


def wrap_text(text: Any, max_chars: int = 48, max_lines: int | None = None) -> list[str]:
    words = str(text or "").replace("\r", "\n").split()
    lines: list[str] = []
    current = ""

    for word in words:
        candidate = word if not current else f"{current} {word}"
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            lines.append(current)
        current = word
        if max_lines is not None and len(lines) >= max_lines:
            break

    if current and (max_lines is None or len(lines) < max_lines):
        lines.append(current)

    return lines


def append_calculator_summary_pages(doc, final_record: dict[str, Any]) -> None:
    calculators = list(final_record.get("saved_calculators", []) or [])
    if not calculators:
        return

    import fitz  # PyMuPDF

    title_color = (0.05, 0.18, 0.34)
    margin = 42
    row_height = 18
    page_width = 792
    page_height = 612

    def new_page():
        page = doc.new_page(width=page_width, height=page_height)
        page.insert_text(
            fitz.Point(margin, 38),
            "APPRAISER CALCULATOR WORKSHEET",
            fontsize=15,
            fontname="helv",
            color=title_color,
        )
        page.insert_text(
            fitz.Point(margin, 58),
            f"Calculated Total Value: ${float(final_record.get('calculated_total_value') or 0.0):,.2f}",
            fontsize=10.5,
            fontname="helv",
            color=(0, 0, 0),
        )
        return page, 84

    page, y = new_page()

    for calculator in calculators:
        section_total = calculator.get("section_total")
        try:
            section_total_text = f"${float(section_total):,.2f}"
        except (TypeError, ValueError):
            section_total_text = str(section_total or "-")

        heading = (
            f"{calculator.get('name') or 'Calculator'} | "
            f"{calculator.get('depreciation_table') or '-'} | "
            f"Tax Year {calculator.get('tax_year') or '-'} | "
            f"Total {section_total_text}"
        )
        if y > page_height - 72:
            page, y = new_page()
        page.insert_text(
            fitz.Point(margin, y),
            heading,
            fontsize=10.5,
            fontname="helv",
            color=title_color,
        )
        y += 14
        page.insert_text(
            fitz.Point(margin, y),
            "Year",
            fontsize=9,
            fontname="helv",
            color=(0, 0, 0),
        )
        page.insert_text(
            fitz.Point(margin + 150, y),
            "Cost",
            fontsize=9,
            fontname="helv",
            color=(0, 0, 0),
        )
        page.insert_text(
            fitz.Point(margin + 290, y),
            "Factor",
            fontsize=9,
            fontname="helv",
            color=(0, 0, 0),
        )
        page.insert_text(
            fitz.Point(margin + 390, y),
            "Value",
            fontsize=9,
            fontname="helv",
            color=(0, 0, 0),
        )
        y += 12

        for row in calculator.get("rows", []) or []:
            if y > page_height - 36:
                page, y = new_page()
            page.insert_text(fitz.Point(margin, y), str(row.get("display_year") or "-"), fontsize=9, fontname="helv")
            page.insert_text(
                fitz.Point(margin + 150, y),
                f"${float(row.get('cost') or 0.0):,.2f}",
                fontsize=9,
                fontname="helv",
            )
            page.insert_text(
                fitz.Point(margin + 290, y),
                f"{float(row.get('factor') or 0.0):.2f}",
                fontsize=9,
                fontname="helv",
            )
            page.insert_text(
                fitz.Point(margin + 390, y),
                f"${float(row.get('value') or 0.0):,.2f}",
                fontsize=9,
                fontname="helv",
            )
            y += row_height

        y += 10


def build_final_review_record(
    file_name: str,
    result: dict[str, Any],
    final_value: Any,
    final_source: str,
    appraiser_notes: str = "",
    appraiser_initials: str = "",
    decision: str = "accepted",
    account_number: str = "",
) -> dict[str, Any]:
    assessment = result.get("assessment_summary", {}) or {}
    agent_review = result.get("agent_review", {}) or {}
    review_flags = result.get("review_flags", {}) or {}
    metadata = result.get("metadata", {}) or {}
    account_number = safe_account_number(account_number or metadata.get("account_number"))

    return {
        "file_name": file_name,
        "account_number": account_number,
        "locked_at": datetime.now().isoformat(timespec="seconds"),
        "decision": decision,
        "final_value": final_value,
        "final_source": final_source,
        "appraiser_initials": appraiser_initials or "",
        "appraiser_notes": appraiser_notes or "",
        "pipeline_recommended_value": get_recommended_value(result),
        "pipeline_value_source": assessment.get("value_source"),
        "pipeline_path": assessment.get("recommended_path"),
        "pipeline_confidence": assessment.get("confidence"),
        "pipeline_issues": assessment.get("issues", []),
        "review_flags": review_flags,
        "agent_status": agent_review.get("status"),
        "agent_confidence": agent_review.get("confidence"),
        "agent_reasoning": agent_review.get("reasoning"),
        "agent_recommended_values": agent_review.get("recommended_values", {}),
    }


def stamp_reviewed_pdf(file_name: str, file_bytes: bytes, final_record: dict[str, Any]) -> Path:
    """
    Stamp page 1 with the locked value, appraiser initials, date, decision,
    and appraiser notes. Long notes are also appended as a final review page.
    The original PDF is not modified.
    """
    ensure_output_dirs()

    import fitz  # PyMuPDF

    account_number = safe_account_number(final_record.get("account_number"))
    stem = account_number or safe_stem(file_name)
    decision = str(final_record.get("decision") or "reviewed").lower()
    out_path = APPRAISER_UPLOAD_DIR / f"{stem}.pdf"

    doc = fitz.open(stream=file_bytes, filetype="pdf")
    if len(doc) == 0:
        raise ValueError("Cannot stamp PDF with no pages.")

    page = doc[0]
    page_width = float(page.rect.width)
    stamp_rect = fitz.Rect(page_width - 265, 28, page_width - 28, 198)

    final_value = final_record.get("final_value")
    try:
        final_value_text = f"${float(final_value):,.2f}"
    except (TypeError, ValueError):
        final_value_text = str(final_value or "-")

    locked_at = str(final_record.get("locked_at") or datetime.now().isoformat(timespec="seconds"))
    date_text = locked_at.split("T", 1)[0]
    decision_label = "ACCEPTED" if decision == "accepted" else "ADJUSTED"
    appraiser_notes = str(final_record.get("appraiser_notes") or "").strip()

    stamp_lines = [
        "APPRAISAL REVIEW",
        f"ACCOUNT: {account_number or '-'}",
        f"VALUE: {final_value_text}",
        f"INITIALS: {final_record.get('appraiser_initials') or '-'}",
        f"DATE: {date_text}",
        f"STATUS: {decision_label}",
    ]
    note_lines = wrap_text(appraiser_notes, max_chars=42, max_lines=4)
    if note_lines:
        stamp_lines.append("NOTE:")
        stamp_lines.extend(note_lines)

    page.draw_rect(stamp_rect, color=(0.05, 0.18, 0.34), fill=(1, 1, 1), width=1.2)
    y = stamp_rect.y0 + 12
    for idx, line in enumerate(stamp_lines):
        fontsize = 9 if idx == 0 else 8.5
        page.insert_text(
            fitz.Point(stamp_rect.x0 + 9, y),
            line,
            fontsize=fontsize,
            fontname="helv",
            color=(0.05, 0.18, 0.34),
        )
        y += 14

    if appraiser_notes:
        notes_page = doc.new_page(width=612, height=792)
        margin = 54
        title_color = (0.05, 0.18, 0.34)
        notes_page.insert_text(
            fitz.Point(margin, 58),
            "APPRAISER REVIEW NOTES",
            fontsize=15,
            fontname="helv",
            color=title_color,
        )
        summary_lines = [
            f"Account: {account_number or '-'}",
            f"Final Value: {final_value_text}",
            f"Source: {final_record.get('final_source') or '-'}",
            f"Initials: {final_record.get('appraiser_initials') or '-'}",
            f"Date: {date_text}",
            f"Status: {decision_label}",
            "",
            "Notes:",
        ]
        y = 92
        for line in summary_lines:
            notes_page.insert_text(
                fitz.Point(margin, y),
                line,
                fontsize=10.5,
                fontname="helv",
                color=(0, 0, 0),
            )
            y += 18

        for line in wrap_text(appraiser_notes, max_chars=88):
            if y > 742:
                notes_page = doc.new_page(width=612, height=792)
                y = 58
            notes_page.insert_text(
                fitz.Point(margin, y),
                line,
                fontsize=10.5,
                fontname="helv",
                color=(0, 0, 0),
            )
            y += 16

    append_calculator_summary_pages(doc, final_record)

    doc.save(out_path, deflate=True, garbage=4)
    doc.close()
    return out_path


def save_review_outputs(file_name: str, result: dict[str, Any], final_record: dict[str, Any] | None = None) -> dict[str, Path]:
    ensure_output_dirs()
    stem = safe_stem(file_name)
    paths = {
        "json": OUTPUT_DIR / f"{stem}_review.json",
        "summary": OUTPUT_DIR / f"{stem}_summary.csv",
    }

    payload = dict(result)
    if final_record:
        payload["final_review"] = final_record

    paths["json"].write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    write_summary_csv(paths["summary"], file_name, result, final_record)

    if final_record:
        final_path = COMPLETED_DIR / f"{stem}_final.json"
        final_path.write_text(json.dumps(final_record, indent=2, default=str), encoding="utf-8")
        paths["final"] = final_path

    return paths


def write_summary_csv(path: Path, file_name: str, result: dict[str, Any], final_record: dict[str, Any] | None = None) -> None:
    assessment = result.get("assessment_summary", {}) or {}
    agent_review = result.get("agent_review", {}) or {}
    form_flags = result.get("form_flags", {}) or {}
    attachments = result.get("attachments", {}) or {}
    schedule_e = result.get("schedule_e", {}) or {}
    metadata = result.get("metadata", {}) or {}

    row = {
        "file_name": file_name,
        "tax_year": metadata.get("tax_year"),
        "owner_name": metadata.get("owner_name"),
        "account_number": metadata.get("account_number"),
        "recommended_value": get_recommended_value(result),
        "value_source": assessment.get("value_source"),
        "recommended_path": assessment.get("recommended_path"),
        "confidence": assessment.get("confidence"),
        "issues": " | ".join(str(x) for x in assessment.get("issues", []) or []),
        "signature_detected": bool(form_flags.get("signature_block_detected")),
        "see_attached": bool(form_flags.get("see_attached")),
        "schedule_e_total": schedule_e.get("total"),
        "best_attachment_total": attachments.get("best_attachment_total"),
        "agent_status": agent_review.get("status"),
        "agent_confidence": agent_review.get("confidence"),
        "agent_flags": " | ".join(str(x) for x in agent_review.get("review_flags", []) or []),
        "final_value": (final_record or {}).get("final_value"),
        "final_source": (final_record or {}).get("final_source"),
        "appraiser_notes": (final_record or {}).get("appraiser_notes"),
        "locked_at": (final_record or {}).get("locked_at"),
    }

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row.keys()))
        writer.writeheader()
        writer.writerow(row)


def append_queue_row(file_name: str, result: dict[str, Any], status: str) -> None:
    ensure_output_dirs()
    assessment = result.get("assessment_summary", {}) or {}
    agent_review = result.get("agent_review", {}) or {}
    metadata = result.get("metadata", {}) or {}
    row = {
        "processed_at": datetime.now().isoformat(timespec="seconds"),
        "file_name": file_name,
        "tax_year": metadata.get("tax_year"),
        "owner_name": metadata.get("owner_name"),
        "account_number": metadata.get("account_number"),
        "status": status,
        "recommended_value": get_recommended_value(result),
        "value_source": assessment.get("value_source"),
        "confidence": assessment.get("confidence"),
        "issues": " | ".join(str(x) for x in assessment.get("issues", []) or []),
        "agent_status": agent_review.get("status"),
    }

    write_header = not QUEUE_CSV.exists()
    with QUEUE_CSV.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row.keys()))
        if write_header:
            writer.writeheader()
        writer.writerow(row)
