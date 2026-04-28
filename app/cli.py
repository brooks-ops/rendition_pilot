from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class OverrideSelection:
    mode: str
    manual_override: dict[str, Any] | None


def safe_get(data: Any, *path: str, default: Any = None) -> Any:
    current = data
    for key in path:
        if not isinstance(current, dict):
            return default
        if key not in current:
            return default
        current = current[key]
    return current


def first_present(data: dict[str, Any], paths: list[tuple[str, ...]], default: Any = None) -> Any:
    for path in paths:
        value = safe_get(data, *path, default=None)
        if value is not None:
            return value
    return default


def format_bool(value: Any) -> str:
    return "YES" if bool(value) else "NO"


def format_money(value: Any) -> str:
    if value is None or value == "":
        return "-"
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return str(value)


def format_percent(value: Any) -> str:
    if value is None or value == "":
        return "-"
    try:
        return f"{float(value):.2%}"
    except (TypeError, ValueError):
        return str(value)


def format_text(value: Any) -> str:
    if value is None or value == "":
        return "-"
    if isinstance(value, list):
        return " | ".join(str(v) for v in value) if value else "-"
    return str(value)


def infer_valuation_path(result: dict[str, Any]) -> str:
    explicit = first_present(
        result,
        [
            ("assessment_summary", "valuation_path"),
            ("assessment_summary", "recommended_valuation_path"),
            ("assessment_summary", "path_used"),
            ("assessment_summary", "recommended_path"),
            ("valuation_summary", "valuation_path"),
            ("valuation_summary", "path_used"),
            ("recommended_valuation_path",),
            ("valuation_path",),
        ],
    )
    if explicit:
        mapping = {
            "use_manual_attachment_total": "Manual Override - Attachment Total",
            "use_manual_good_faith_value": "Manual Override - Good Faith Value",
            "use_manual_historical_cost_depreciated": "Manual Override - Historical Cost Less Depreciation",
            "use_attachment_total_pending_review": "Attachment Total",
            "use_schedule_total_pending_review": "Schedule E Total",
            "use_good_faith_value_pending_review": "Good Faith Estimate",
            "manual_review": "Manual Review",
        }
        return mapping.get(str(explicit), str(explicit))

    manual_override = safe_get(result, "manual_override", default={}) or {}
    if manual_override.get("attachment_total") is not None:
        return "Manual Override - Attachment Total"
    if manual_override.get("good_faith_value") is not None:
        return "Manual Override - Good Faith Value"
    if manual_override.get("historical_cost") is not None:
        return "Manual Override - Historical Cost Less Depreciation"

    schedule_e_total = first_present(
        result,
        [
            ("schedule_e", "total"),
            ("schedule_e_total",),
            ("parsed_values", "schedule_e_total"),
        ],
    )
    if schedule_e_total is not None:
        return "Schedule E Total"

    best_attachment_total = safe_get(result, "attachments", "best_attachment_total")
    if best_attachment_total is not None:
        return "Attachment Total"

    return "Auto / Recommended"


def infer_recommended_value(result: dict[str, Any]) -> Any:
    return first_present(
        result,
        [
            ("assessment_summary", "recommended_value"),
            ("assessment_summary", "recommended_market_value"),
            ("assessment_summary", "recommended_assessed_value"),
            ("assessment_summary", "extracted_value"),
            ("valuation_summary", "recommended_value"),
            ("valuation_summary", "value_used"),
            ("final_value",),
            ("recommended_value",),
        ],
    )


def infer_path_reason(result: dict[str, Any]) -> str:
    reason = first_present(
        result,
        [
            ("assessment_summary", "reason"),
            ("assessment_summary", "valuation_reason"),
            ("valuation_summary", "reason"),
            ("recommended_reason",),
        ],
    )
    if reason:
        return format_text(reason)

    issues = safe_get(result, "assessment_summary", "issues", default=[])
    if issues:
        return format_text(issues)

    return "-"


def build_cli_summary(result: dict[str, Any], source_path: str | None = None) -> str:
    form_flags = safe_get(result, "form_flags", default={}) or {}
    attachments = safe_get(result, "attachments", default={}) or {}
    manual_override = safe_get(result, "manual_override", default={}) or {}
    schedule_e = safe_get(result, "schedule_e", default={}) or {}
    assessment_summary = safe_get(result, "assessment_summary", default={}) or {}
    depreciated_override_result = safe_get(result, "depreciated_override_result", default={}) or {}
    agent_review = safe_get(result, "agent_review", default={}) or {}
    review_flags = safe_get(result, "review_flags", default={}) or {}
    agent_values = safe_get(agent_review, "recommended_values", default={}) or {}

    schedule_e_total = first_present(
        result,
        [
            ("schedule_e", "total"),
            ("schedule_e_total",),
            ("parsed_values", "schedule_e_total"),
        ],
    )

    valuation_path = infer_valuation_path(result)
    recommended_value = infer_recommended_value(result)
    path_reason = infer_path_reason(result)
    processed_at = first_present(result, [("processed_at",)], default="-")

    lines: list[str] = []
    lines.append("=" * 78)
    lines.append("BPP RENDITION REVIEW SUMMARY")
    lines.append("=" * 78)
    lines.append(f"Source File:            {format_text(source_path)}")
    lines.append(f"Processed At:           {format_text(processed_at)}")
    lines.append("")

    lines.append("-" * 78)
    lines.append("FORM FLAGS")
    lines.append("-" * 78)
    lines.append(f"Section 5 Present:      {format_bool(form_flags.get('section_5_present'))}")
    lines.append(f"Over $20k Language:     {format_bool(form_flags.get('section_5_over_20k_detected'))}")
    lines.append(f"$125k Language:         {format_bool(form_flags.get('section_5_125k_language_detected'))}")
    lines.append(f"Signature Detected:     {format_bool(form_flags.get('signature_block_detected'))}")
    lines.append(f"SEE ATTACHED:           {format_bool(form_flags.get('see_attached'))}")
    lines.append("")

    lines.append("-" * 78)
    lines.append("SCHEDULES / ATTACHMENTS")
    lines.append("-" * 78)
    lines.append(f"Schedule E Present:     {format_bool(schedule_e.get('schedule_e_present'))}")
    lines.append(f"Schedule E Total:       {format_money(schedule_e_total)}")
    lines.append(f"M&E on Schedule E:      {format_bool(schedule_e.get('machinery_and_equipment_present'))}")
    lines.append(f"Attachment Summary:     {format_bool(attachments.get('attachment_summary_present'))}")
    lines.append(f"Best Attachment Total:  {format_money(attachments.get('best_attachment_total'))}")
    lines.append(f"Current Value Found:    {format_bool(attachments.get('current_value_detected'))}")
    lines.append(f"Reported Cost Found:    {format_bool(attachments.get('reported_cost_detected'))}")
    lines.append(f"Rendered Value Found:   {format_bool(attachments.get('rendered_value_detected'))}")
    lines.append(f"M&E Present:            {format_bool(attachments.get('machinery_and_equipment_present'))}")
    lines.append("")

    lines.append("-" * 78)
    lines.append("MANUAL OVERRIDE INPUTS")
    lines.append("-" * 78)
    lines.append(f"Attachment Total:       {format_money(manual_override.get('attachment_total'))}")
    lines.append(f"Good Faith Value:       {format_money(manual_override.get('good_faith_value'))}")
    lines.append(f"Historical Cost:        {format_money(manual_override.get('historical_cost'))}")
    lines.append(f"Acquisition Year:       {format_text(manual_override.get('acquisition_year'))}")
    lines.append(f"Life Years:             {format_text(manual_override.get('life_years'))}")
    lines.append(f"Notes:                  {format_text(manual_override.get('notes'))}")
    lines.append("")

    lines.append("-" * 78)
    lines.append("VALUATION DECISION")
    lines.append("-" * 78)
    lines.append(f"Valuation Path Used:    {format_text(valuation_path)}")
    lines.append(f"Recommended Value:      {format_money(recommended_value)}")
    lines.append(f"Value Source:           {format_text(assessment_summary.get('value_source'))}")
    lines.append(f"Percent Good:           {format_percent(depreciated_override_result.get('percent_good'))}")
    lines.append(f"Reason:                 {path_reason}")
    lines.append(f"OCR Provider:           {format_text(assessment_summary.get('ocr_provider_used') or review_flags.get('ocr_provider_used'))}")
    lines.append(f"Provider Agreement:     {format_bool(review_flags.get('provider_agreement'))}")
    lines.append(f"Agreement Fields:       {format_text(assessment_summary.get('provider_agreement_fields') or review_flags.get('provider_agreement_fields'))}")
    lines.append(f"Issues:                 {format_text(assessment_summary.get('issues'))}")
    lines.append("")

    lines.append("-" * 78)
    lines.append("AI AGENT REVIEW")
    lines.append("-" * 78)
    lines.append(f"Status:                 {format_text(agent_review.get('status'))}")
    lines.append(f"Recommended Source:     {format_text(agent_values.get('selected_source'))}")
    lines.append(f"Historical Cost:        {format_money(agent_values.get('historical_cost'))}")
    lines.append(f"Acquisition Year:       {format_text(agent_values.get('acquisition_year'))}")
    lines.append(f"Rendered Value:         {format_money(agent_values.get('rendered_value'))}")
    lines.append(f"Good Faith Value:       {format_money(agent_values.get('good_faith_value'))}")
    lines.append(f"Attachment Total:       {format_money(agent_values.get('attachment_total'))}")
    lines.append(f"Confidence:             {format_text(agent_review.get('confidence'))}")
    lines.append(f"Flags:                  {format_text(agent_review.get('review_flags'))}")
    lines.append(f"Reasoning:              {format_text(agent_review.get('reasoning'))}")
    lines.append("=" * 78)

    return "\n".join(lines)


def _prompt_float(label: str, allow_blank: bool = False) -> float | None:
    while True:
        raw = input(f"{label}: ").strip()
        if allow_blank and raw == "":
            return None
        try:
            return float(raw.replace(",", "").replace("$", ""))
        except ValueError:
            print("Enter a valid number.")


def _prompt_int(label: str, allow_blank: bool = False) -> int | None:
    while True:
        raw = input(f"{label}: ").strip()
        if allow_blank and raw == "":
            return None
        try:
            return int(raw)
        except ValueError:
            print("Enter a valid whole number.")


def prompt_override_selection() -> OverrideSelection:
    print("\nSelect valuation mode:")
    print("  1) Auto / recommended path")
    print("  2) Force attachment total")
    print("  3) Force good faith value")
    print("  4) Force historical cost less depreciation")
    print("  5) No override prompt, continue with auto")

    while True:
        choice = input("\nEnter choice (1-5): ").strip()
        if choice in {"1", "5"}:
            return OverrideSelection(mode="auto", manual_override=None)

        if choice == "2":
            attachment_total = _prompt_float("Attachment total")
            notes = input("Notes (optional): ").strip() or None
            return OverrideSelection(
                mode="attachment_total",
                manual_override={
                    "attachment_total": attachment_total,
                    "good_faith_value": None,
                    "historical_cost": None,
                    "acquisition_year": None,
                    "life_years": None,
                    "notes": notes,
                },
            )

        if choice == "3":
            good_faith_value = _prompt_float("Good faith value")
            notes = input("Notes (optional): ").strip() or None
            return OverrideSelection(
                mode="good_faith_value",
                manual_override={
                    "attachment_total": None,
                    "good_faith_value": good_faith_value,
                    "historical_cost": None,
                    "acquisition_year": None,
                    "life_years": None,
                    "notes": notes,
                },
            )

        if choice == "4":
            historical_cost = _prompt_float("Historical cost")
            acquisition_year = _prompt_int("Acquisition year")
            life_years = _prompt_int("Life years")
            notes = input("Notes (optional): ").strip() or None
            return OverrideSelection(
                mode="historical_cost",
                manual_override={
                    "attachment_total": None,
                    "good_faith_value": None,
                    "historical_cost": historical_cost,
                    "acquisition_year": acquisition_year,
                    "life_years": life_years,
                    "notes": notes,
                },
            )

        print("Choose 1, 2, 3, 4, or 5.")
