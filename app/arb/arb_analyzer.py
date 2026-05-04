from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from app.arb.arb_models import ARBCaseInfo, ARBParsedPacket, ARBReviewSummary

logger = logging.getLogger(__name__)

ARB_SYSTEM_PROMPT = """You are assisting a Texas commercial appraisal district appraiser preparing for an ARB protest hearing. Review the CAD evidence and taxpayer/agent evidence side by side. Identify the strongest and weakest points from each side. Focus on evidence quality, appraisal relevance, and hearing usefulness. Do not invent facts. If evidence is missing, say so. Provide a practical hearing strategy and suggested settlement range if supported by the evidence.

The analysis supports appraiser preparation and does not replace appraiser judgment. Do not claim legal certainty.

Commercial appraisal focus areas include income approach support, market rent, expense ratio, cap rate, vacancy and collection loss, sales comps, equity comps, property condition, deferred maintenance, occupancy, unsupported owner statements, stale comps, non-arm's-length sales, mismatched property types, location differences, size/age/class differences, whether the agent evidence supports a lower value or only raises doubt, and whether CAD evidence directly supports noticed value.

Value recommendation rules:
- If the stronger evidence favors CAD, recommend holding CAD proposed value, or current noticed value if no CAD proposed value is supplied.
- If the taxpayer evidence is much stronger and includes credible quantified support, recommend the taxpayer requested value or a value close to it.
- If both sides have credible support, recommend a reasoned midpoint or narrow settlement range between supported anchors. Explain why.
- If evidence is weak or incomplete, do not invent a value. Recommend appraiser review, more evidence, or defending the CAD value.
- Every final recommendation must state that the appraiser should review and approve the final value."""


CASE_FIELD_PATTERNS: dict[str, list[str]] = {
    "property_owner": [
        r"(?:property\s+owner|owner\s+name|owner)\s*[:#-]\s*([^\n\r]{3,90})",
        r"(?:taxpayer|owner/taxpayer)\s*[:#-]\s*([^\n\r]{3,90})",
    ],
    "property_address": [
        r"(?:property\s+address|situs\s+address|location\s+address|address)\s*[:#-]\s*([^\n\r]{6,120})",
    ],
    "property_type": [
        r"(?:property\s+type|property\s+class|class|use\s+type)\s*[:#-]\s*([^\n\r]{3,80})",
    ],
    "tax_year": [
        r"(?:tax\s+year|appraisal\s+year|year)\s*[:#-]\s*((?:20)\d{2})",
    ],
    "current_noticed_value": [
        r"(?:current\s+noticed\s+value|noticed\s+value|notice\s+value|appraised\s+value|market\s+value)\s*[:#-]?\s*(\$?\s?\d[\d,]*(?:\.\d{2})?)",
    ],
    "agent_requested_value": [
        r"(?:agent\s+requested\s+value|taxpayer\s+requested\s+value|requested\s+value|opinion\s+of\s+value|settlement\s+request)\s*[:#-]?\s*(\$?\s?\d[\d,]*(?:\.\d{2})?)",
    ],
    "cad_proposed_value": [
        r"(?:cad\s+proposed\s+value|district\s+proposed\s+value|cad\s+value|recommended\s+value)\s*[:#-]?\s*(\$?\s?\d[\d,]*(?:\.\d{2})?)",
    ],
}


def infer_case_info(
    cad_packet: ARBParsedPacket,
    taxpayer_packet: ARBParsedPacket,
    user_case_info: ARBCaseInfo,
) -> ARBCaseInfo:
    combined_text = f"{cad_packet.text}\n{taxpayer_packet.text}"
    inferred = ARBCaseInfo()
    account = _extract_account_number(combined_text)
    if account:
        inferred.account_number = account
    source_text_by_field = {
        "current_noticed_value": cad_packet.text,
        "cad_proposed_value": cad_packet.text,
        "agent_requested_value": taxpayer_packet.text,
    }
    for field_name, patterns in CASE_FIELD_PATTERNS.items():
        source_text = source_text_by_field.get(field_name) or combined_text
        value = _first_pattern_match(combined_text, patterns)
        source_value = _first_pattern_match(source_text, patterns)
        value = source_value or value
        if value:
            setattr(inferred, field_name, _clean_case_value(value))
    return merge_case_info(user_case_info, inferred)


def merge_case_info(user_case_info: ARBCaseInfo, inferred_case_info: ARBCaseInfo) -> ARBCaseInfo:
    data: dict[str, str] = {}
    user_data = user_case_info.model_dump()
    inferred_data = inferred_case_info.model_dump()
    for key in user_data:
        user_value = str(user_data.get(key) or "").strip()
        inferred_value = str(inferred_data.get(key) or "").strip()
        data[key] = user_value or inferred_value
    return ARBCaseInfo(**data)


def analyze_arb_evidence(
    cad_packet: ARBParsedPacket,
    taxpayer_packet: ARBParsedPacket,
    case_info: ARBCaseInfo,
) -> ARBReviewSummary:
    fallback = _fallback_analysis(cad_packet, taxpayer_packet, case_info)
    fallback.warnings.append("OpenAI ARB analysis disabled on local-no-ai branch; deterministic ARB review was used.")
    return fallback


def build_summary_text(summary: ARBReviewSummary, case_info: ARBCaseInfo) -> str:
    lines = [
        "ARB Review Summary",
        "",
        f"Account number: {case_info.account_number or '-'}",
        f"Property owner: {case_info.property_owner or '-'}",
        f"Property address: {case_info.property_address or '-'}",
        f"Tax year: {case_info.tax_year or '-'}",
        f"Current noticed value: {case_info.current_noticed_value or '-'}",
        f"CAD proposed value: {case_info.cad_proposed_value or '-'}",
        f"Agent requested value: {case_info.agent_requested_value or '-'}",
        "",
        "CAD Evidence Summary",
        summary.cad_summary or "-",
        "",
        "Agent / Taxpayer Evidence Summary",
        summary.taxpayer_summary or "-",
        "",
        "Strongest CAD Points",
        *_bullets(summary.cad_strong_points),
        "",
        "Weakest CAD Points",
        *_bullets(summary.cad_weak_points),
        "",
        "Strongest Agent / Taxpayer Points",
        *_bullets(summary.taxpayer_strong_points),
        "",
        "Weakest Agent / Taxpayer Points",
        *_bullets(summary.taxpayer_weak_points),
        "",
        "Rebuttal Points",
        *_bullets(summary.rebuttal_points),
        "",
        "Suggested Value / Settlement Range",
        f"Suggested value: {summary.suggested_value or '-'}",
        f"Settlement range: {summary.settlement_range or '-'}",
        "",
        "Missing Evidence / Follow-Up",
        *_bullets(summary.missing_evidence),
        "",
        "ARB Hearing Prep",
        *_bullets(summary.hearing_strategy),
        "",
        "Final Recommendation",
        summary.final_recommendation or "-",
    ]
    return "\n".join(lines)


def _fallback_analysis(
    cad_packet: ARBParsedPacket,
    taxpayer_packet: ARBParsedPacket,
    case_info: ARBCaseInfo,
) -> ARBReviewSummary:
    cad_text = cad_packet.text
    taxpayer_text = taxpayer_packet.text
    values = _extract_values(cad_text + "\n" + taxpayer_text)
    provided_values = _provided_values(case_info)
    evidence = _evidence_assessment(cad_text, taxpayer_text)
    warnings = _dedupe([*cad_packet.warnings, *taxpayer_packet.warnings])

    summary = ARBReviewSummary(
        cad_summary=_basic_summary("CAD evidence", cad_text),
        taxpayer_summary=_basic_summary("Agent / taxpayer evidence", taxpayer_text),
        cad_strong_points=_strong_points(cad_text, "CAD"),
        cad_weak_points=_weak_points(cad_text, "CAD"),
        taxpayer_strong_points=_strong_points(taxpayer_text, "agent / taxpayer"),
        taxpayer_weak_points=_weak_points(taxpayer_text, "agent / taxpayer"),
        rebuttal_points=_rebuttal_points(taxpayer_text),
        suggested_value=_suggested_value_text(provided_values, values, evidence),
        settlement_range=_settlement_range_text(provided_values, evidence),
        missing_evidence=_missing_evidence(cad_text, taxpayer_text),
        hearing_strategy=[
            "Confirm that all value conclusions tie to the January 1 valuation date.",
            "Lead with documented income, sales, equity, and property characteristic evidence rather than unsupported assertions.",
            "Ask the agent to connect any requested reduction to market data and quantified value impact.",
        ],
        final_recommendation=_final_recommendation(cad_text, taxpayer_text, provided_values, evidence),
        warnings=warnings,
    )
    return summary


def _basic_summary(label: str, text: str) -> str:
    if not text.strip():
        return f"{label} text was not available for review."
    terms = _term_hits(text)
    if terms:
        return f"{label} contains references to {', '.join(terms[:8])}. Review should verify whether these references include support, dates, and property-specific value conclusions."
    return f"{label} was extracted, but the deterministic review did not identify specific commercial appraisal themes. Use the extracted text preview for manual review."


def _strong_points(text: str, side: str) -> list[str]:
    points: list[str] = []
    lowered = text.lower()
    checks = [
        ("income approach", "Includes income approach discussion that may support value if rents, expenses, vacancy, and cap rate are documented."),
        ("rent roll", "Includes rent roll information that may support occupancy and income assumptions when lease context is provided."),
        ("cap rate", "References cap rate evidence, useful if tied to market extraction or credible surveys."),
        ("sale", "Includes sales evidence that may support market value if comparable in date, location, class, age, size, and condition."),
        ("equity", "Includes equity comparison evidence that may support uniformity arguments if comparables are matched and adjusted."),
        ("photo", "Includes photos that may support condition claims if the market impact is quantified."),
        ("repair", "Includes repair or deferred maintenance information that may support condition adjustments if verified and market-based."),
    ]
    for marker, point in checks:
        if marker in lowered:
            points.append(f"{side}: {point}")
    if not points:
        points.append(f"{side}: No clear strong commercial appraisal support was identified in extracted text.")
    return points[:6]


def _weak_points(text: str, side: str) -> list[str]:
    points: list[str] = []
    lowered = text.lower()
    if "cap rate" not in lowered:
        points.append(f"{side}: Cap rate support was not clearly identified.")
    if not any(term in lowered for term in ["rent roll", "lease", "market rent"]):
        points.append(f"{side}: Market rent or lease support was not clearly identified.")
    if not any(term in lowered for term in ["sale", "comparable", "comp"]):
        points.append(f"{side}: Sales comparison support was not clearly identified.")
    if "january 1" not in lowered and "jan 1" not in lowered:
        points.append(f"{side}: Evidence date connection to January 1 was not clearly identified.")
    if not text.strip():
        points.append(f"{side}: No extracted text is available; OCR or manual review is needed.")
    return points[:6]


def _rebuttal_points(taxpayer_text: str) -> list[str]:
    lowered = taxpayer_text.lower()
    points = [
        "Requested value must be tied to market evidence, not only disagreement with the noticed value.",
        "Photos or repair estimates show condition but do not automatically equal dollar-for-dollar value loss without market impact support.",
        "Owner-provided income, expenses, or repair costs should be verified and normalized to market assumptions.",
        "Rent roll data does not prove market rent without lease terms, concessions, occupancy history, and market context.",
        "Sales or equity comps should be tested for date, location, use, class, age, size, condition, and arm's-length status.",
        "Evidence should address the January 1 valuation date or explain why later data is relevant.",
    ]
    if "cap rate" in lowered:
        points.append("Challenge whether the cap rate is market-derived, property-type appropriate, and applied to stabilized income.")
    if any(term in lowered for term in ["expense", "noi", "income"]):
        points.append("Ask whether income and expense data is actual, stabilized, market-based, and supported by source documents.")
    return points[:8]


def _missing_evidence(cad_text: str, taxpayer_text: str) -> list[str]:
    combined = (cad_text + "\n" + taxpayer_text).lower()
    missing: list[str] = []
    checks = [
        ("market rent support", ["market rent", "lease", "rent roll"]),
        ("expense support", ["expense", "noi", "income statement"]),
        ("cap rate support", ["cap rate"]),
        ("sales comparable adjustment support", ["sale", "comparable", "adjustment"]),
        ("January 1 valuation date support", ["january 1", "jan 1"]),
        ("property condition quantification", ["condition", "repair", "deferred maintenance"]),
    ]
    for label, terms in checks:
        if not any(term in combined for term in terms):
            missing.append(label)
    return missing[:8]


def _final_recommendation(
    cad_text: str,
    taxpayer_text: str,
    provided_values: dict[str, str],
    evidence: dict[str, Any],
) -> str:
    if not cad_text.strip() or not taxpayer_text.strip():
        return "request more evidence; final value remains subject to appraiser review"
    if evidence["lean"] == "taxpayer":
        return "consider reduction toward the taxpayer supported value; final value remains subject to appraiser review"
    if evidence["lean"] == "mixed":
        return "consider settlement within the supported range; final value remains subject to appraiser review"
    return "defend CAD value unless appraiser review identifies unsupported CAD assumptions"


def _suggested_value_text(
    provided_values: dict[str, str],
    extracted_values: list[str],
    evidence: dict[str, Any],
) -> str:
    cad_value = provided_values.get("cad_proposed_value") or provided_values.get("current_noticed_value")
    agent_value = provided_values.get("agent_requested_value")
    if evidence["lean"] == "cad" and cad_value:
        return f"Recommended value: {cad_value}. The extracted evidence favors CAD, so hold the CAD proposed/current noticed value unless appraiser review finds unsupported assumptions."
    if evidence["lean"] == "taxpayer" and agent_value:
        return f"Recommended value: {agent_value}, or close to it if appraiser review confirms the taxpayer evidence is credible, quantified, and market-based."
    if evidence["lean"] == "mixed" and cad_value and agent_value:
        midpoint = _money_midpoint(cad_value, agent_value)
        if midpoint:
            return f"Recommended review value: {midpoint}. Both sides show support, so use this as a starting midpoint between the CAD anchor ({cad_value}) and taxpayer anchor ({agent_value}), subject to appraiser review of evidence quality."
        return f"Recommended value should be negotiated between CAD anchor ({cad_value}) and taxpayer anchor ({agent_value}) based on appraiser review of the strongest supported adjustments."
    if cad_value:
        return f"Recommended value: {cad_value}. Taxpayer evidence was not strong enough in the extracted text to support a specific reduction without appraiser review."
    if extracted_values:
        return f"Values were found in the packets ({', '.join(extracted_values[:5])}), but no exact suggested value should be adopted without appraiser verification."
    return "No evidence-based exact value can be suggested from extracted text alone."


def _settlement_range_text(provided_values: dict[str, str], evidence: dict[str, Any]) -> str:
    cad_value = provided_values.get("cad_proposed_value") or provided_values.get("current_noticed_value")
    agent_value = provided_values.get("agent_requested_value")
    if cad_value and agent_value:
        if evidence["lean"] == "cad":
            return f"Settlement range should stay at or near the CAD value ({cad_value}) unless appraiser review finds the taxpayer evidence supports a quantified adjustment."
        if evidence["lean"] == "taxpayer":
            return f"Settlement range should move toward taxpayer requested value ({agent_value}) if appraiser review confirms the taxpayer evidence is stronger and quantified; upper bound remains the CAD value ({cad_value})."
        return f"Settlement range should be between taxpayer requested value ({agent_value}) and CAD value ({cad_value}), narrowed by the appraiser to the best-supported adjustments."
    if cad_value:
        return f"No taxpayer anchor value was provided. Settlement range should remain near {cad_value} unless verified evidence supports a reduction."
    return "No settlement range can be recommended without supported value anchors."


def _provided_values(case_info: ARBCaseInfo) -> dict[str, str]:
    return {
        "current_noticed_value": case_info.current_noticed_value.strip(),
        "cad_proposed_value": case_info.cad_proposed_value.strip(),
        "agent_requested_value": case_info.agent_requested_value.strip(),
    }


def _extract_values(text: str) -> list[str]:
    return _dedupe(re.findall(r"\$\s?\d[\d,]*(?:\.\d{2})?", text))[:10]


def _term_hits(text: str) -> list[str]:
    lowered = text.lower()
    labels = [
        ("income approach", "income approach"),
        ("market rent", "market rent"),
        ("rent roll", "rent roll"),
        ("expense ratio", "expense"),
        ("cap rate", "cap rate"),
        ("vacancy", "vacancy"),
        ("sales comps", "sale"),
        ("equity comps", "equity"),
        ("condition", "condition"),
        ("deferred maintenance", "deferred maintenance"),
        ("photos", "photo"),
        ("prior year value", "prior year"),
        ("noticed value", "noticed value"),
    ]
    return [label for label, marker in labels if marker in lowered]


def _evidence_assessment(cad_text: str, taxpayer_text: str) -> dict[str, Any]:
    cad_score = _evidence_score(cad_text, "cad")
    taxpayer_score = _evidence_score(taxpayer_text, "taxpayer")
    if cad_score >= taxpayer_score + 2:
        lean = "cad"
    elif taxpayer_score >= cad_score + 3:
        lean = "taxpayer"
    else:
        lean = "mixed"
    return {"cad_score": cad_score, "taxpayer_score": taxpayer_score, "lean": lean}


def _evidence_score(text: str, side: str) -> int:
    lowered = text.lower()
    score = 0
    weighted_terms = {
        "income approach": 2,
        "market rent": 2,
        "rent roll": 1,
        "income statement": 2,
        "noi": 2,
        "expense": 1,
        "cap rate": 2,
        "vacancy": 1,
        "sale": 2,
        "comparable": 2,
        "equity": 2,
        "january 1": 2,
        "jan 1": 2,
        "adjustment": 1,
        "photo": 1,
        "repair": 1,
        "deferred maintenance": 1,
    }
    for term, weight in weighted_terms.items():
        if term in lowered:
            score += weight
    if side == "taxpayer" and any(term in lowered for term in ["requested value", "opinion of value", "reduction"]):
        score += 1
    if side == "cad" and any(term in lowered for term in ["noticed value", "appraised value", "market value", "cad proposed"]):
        score += 1
    return score


def _money_midpoint(first: str, second: str) -> str:
    first_value = _parse_money(first)
    second_value = _parse_money(second)
    if first_value is None or second_value is None:
        return ""
    midpoint = round((first_value + second_value) / 2)
    return f"${midpoint:,.0f}"


def _parse_money(value: str) -> float | None:
    try:
        return float(re.sub(r"[^0-9.]", "", value))
    except (TypeError, ValueError):
        return None


def _extract_account_number(text: str) -> str:
    patterns = [
        r"\b(?:account|acct|property\s+id|parcel|account\s+number)\s*[:#-]?\s*(R\s*#?\s*\d[\dA-Za-z-]*)\b",
        r"\b(R\s*#?\s*\d[\dA-Za-z-]*)\b",
    ]
    match = _first_pattern_match(text, patterns)
    if not match:
        return ""
    compact = re.sub(r"\s+", "", match).upper()
    compact = compact.replace("R#", "R").replace("#", "")
    if compact.startswith("R") and len(compact) > 1:
        return f"R#{compact[1:]}"
    return compact


def _first_pattern_match(text: str, patterns: list[str]) -> str:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return str(match.group(1) or "").strip()
    return ""


def _clean_case_value(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", value).strip(" :-\t")
    cleaned = re.split(r"\s{2,}| {0,1}\| {0,1}", cleaned)[0].strip()
    return cleaned[:120]


def _packet_for_prompt(packet: ARBParsedPacket) -> dict[str, Any]:
    return {
        "file_name": packet.file_name,
        "packet_label": packet.packet_label,
        "extraction_provider": packet.extraction_provider,
        "warnings": packet.warnings,
        "text": _truncate(packet.text, 55000),
    }


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = text[: int(limit * 0.7)]
    tail = text[-int(limit * 0.3) :]
    return f"{head}\n\n[...truncated for model context...]\n\n{tail}"


def _review_schema() -> dict[str, Any]:
    string_array = {"type": "array", "items": {"type": "string"}}
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "cad_summary": {"type": "string"},
            "taxpayer_summary": {"type": "string"},
            "cad_strong_points": string_array,
            "cad_weak_points": string_array,
            "taxpayer_strong_points": string_array,
            "taxpayer_weak_points": string_array,
            "rebuttal_points": string_array,
            "suggested_value": {"type": "string"},
            "settlement_range": {"type": "string"},
            "missing_evidence": string_array,
            "hearing_strategy": string_array,
            "final_recommendation": {"type": "string"},
            "analysis_status": {"type": "string"},
            "warnings": string_array,
        },
        "required": [
            "cad_summary",
            "taxpayer_summary",
            "cad_strong_points",
            "cad_weak_points",
            "taxpayer_strong_points",
            "taxpayer_weak_points",
            "rebuttal_points",
            "suggested_value",
            "settlement_range",
            "missing_evidence",
            "hearing_strategy",
            "final_recommendation",
            "analysis_status",
            "warnings",
        ],
    }


def _bullets(items: list[str]) -> list[str]:
    return [f"- {item}" for item in items] or ["-"]


def _dedupe(items: list[str]) -> list[str]:
    result: list[str] = []
    for item in items:
        cleaned = str(item or "").strip()
        if cleaned and cleaned not in result:
            result.append(cleaned)
    return result
