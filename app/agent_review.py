import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional


SYSTEM_PROMPT = """
You are a Business Personal Property rendition review agent.

You are reviewing the output of an existing extraction pipeline.
Do NOT invent values.
Do NOT rewrite the entire parse.
Your job is to review candidates, flags, and attachment findings, then recommend the best final values.

Priorities:
1. Prefer explicit values over guesses.
2. If the main form says "see attached" and the attachment appears to contain the detailed asset schedule or totals,
   attachment-based totals may be stronger than weak main-form candidates.
3. If multiple OCR providers agree on a value, treat that consensus as strong evidence.
4. If OCR providers disagree, compare the disagreement against the candidate evidence and explain which provider(s) appear more reliable.
5. If multiple candidates conflict, choose the strongest one and explain why.
6. If confidence is low, return null for the field and add a review flag.
7. Respect manual overrides if present. If a manual override exists, treat it as locked unless clearly marked otherwise.

Return only valid JSON matching the requested schema.
""".strip()


def _safe_get(d: Any, key: str, default=None):
    if isinstance(d, dict):
        return d.get(key, default)
    return default


def _get_openai_api_key() -> Optional[str]:
    api_key = os.getenv("OPENAI_API_KEY")
    if api_key:
        return api_key.strip()

    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return None

    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            key, sep, value = line.partition("=")
            if sep and key.strip() == "OPENAI_API_KEY":
                return value.strip().strip('"').strip("'") or None
    except Exception:
        return None

    return None


def _normalize_candidate(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _collect_top_candidates(parse_result: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """
    Pull likely candidate lists out of your current parse_result shape.
    This is intentionally defensive since your structure has evolved over time.
    """
    candidates: Dict[str, List[Dict[str, Any]]] = {
        "historical_cost": [],
        "acquisition_year": [],
        "rendered_value": [],
        "good_faith_value": [],
        "attachment_total": [],
    }

    # Direct candidate buckets if they already exist
    candidate_buckets = _safe_get(parse_result, "candidates", {}) or {}
    for field in candidates.keys():
        raw_list = candidate_buckets.get(field, [])
        if isinstance(raw_list, list):
            for item in raw_list:
                if isinstance(item, dict):
                    value = _normalize_candidate(item.get("value"))
                    if value:
                        candidates[field].append(item)

    # Manual override values also matter
    manual_override = _safe_get(parse_result, "manual_override", {}) or {}
    override_map = {
        "historical_cost": manual_override.get("historical_cost"),
        "acquisition_year": manual_override.get("acquisition_year"),
        "good_faith_value": manual_override.get("good_faith_value"),
        "attachment_total": manual_override.get("attachment_total"),
    }
    for field, value in override_map.items():
        value = _normalize_candidate(value)
        if value:
            candidates[field].insert(
                0,
                {
                    "value": value,
                    "source": "manual_override",
                    "evidence_text": "Value supplied by manual override.",
                    "confidence": 1.0,
                },
            )

    # Existing best-known fields from prior pipeline passes
    attachments = _safe_get(parse_result, "attachments", {}) or {}
    best_attachment_total = attachments.get("best_attachment_total")
    if _normalize_candidate(best_attachment_total):
        candidates["attachment_total"].insert(
            0,
            {
                "value": str(best_attachment_total),
                "source": "attachments.best_attachment_total",
                "evidence_text": "Best attachment total found by existing pipeline.",
                "confidence": 0.99,
                "score": 100.0,
            },
        )

    # If your old pipeline stored direct top-level values anywhere, add them here
    for field in ["historical_cost", "acquisition_year", "rendered_value", "good_faith_value"]:
        top_level_value = parse_result.get(field)
        if _normalize_candidate(top_level_value):
            candidates[field].insert(
                0,
                {
                    "value": str(top_level_value),
                    "source": f"top_level.{field}",
                    "evidence_text": "Top-level value from existing pipeline output.",
                    "confidence": 0.8,
                },
            )

    return candidates


def _allowed_values(agent_input: Dict[str, Any]) -> Dict[str, set[str]]:
    allowed: Dict[str, set[str]] = {}
    candidate_values = agent_input.get("candidate_values", {}) or {}
    if not isinstance(candidate_values, dict):
        return allowed
    for field, candidates in candidate_values.items():
        field_values: set[str] = set()
        if isinstance(candidates, list):
            for candidate in candidates:
                if isinstance(candidate, dict):
                    value = _normalize_candidate(candidate.get("value"))
                    if value:
                        field_values.add(value)
                        try:
                            field_values.add(str(float(str(value).replace(",", "").replace("$", ""))))
                        except Exception:
                            pass
        allowed[field] = field_values
    return allowed


def _guardrail_review(result: Dict[str, Any], agent_input: Dict[str, Any]) -> Dict[str, Any]:
    allowed = _allowed_values(agent_input)
    recommended = result.get("recommended_values", {})
    if not isinstance(recommended, dict):
        result["recommended_values"] = {}
        result.setdefault("review_flags", []).append("agent_review_bad_recommended_values")
        return result

    flags = result.get("review_flags", [])
    if not isinstance(flags, list):
        flags = []

    manual_override = agent_input.get("manual_override", {}) or {}
    for field in ["historical_cost", "acquisition_year", "rendered_value", "good_faith_value", "attachment_total"]:
        manual_value = _normalize_candidate(manual_override.get(field))
        if manual_value:
            recommended[field] = manual_value
            continue

        value = _normalize_candidate(recommended.get(field))
        if value is None:
            recommended[field] = None
            continue

        normalized_value = value
        try:
            normalized_value = str(float(value.replace(",", "").replace("$", "")))
        except Exception:
            pass

        if value not in allowed.get(field, set()) and normalized_value not in allowed.get(field, set()):
            recommended[field] = None
            flags.append(f"agent_rejected_unseen_{field}")

    result["recommended_values"] = recommended
    result["review_flags"] = sorted(set(flags))
    return result


def _candidate_sort_key(candidate: Dict[str, Any]) -> float:
    try:
        confidence = candidate.get("confidence")
        if confidence is not None:
            return float(confidence)
        score = float(candidate.get("score", 0) or 0)
        return score / 30 if score > 1 else score
    except Exception:
        return 0.0


def _fallback_review(parse_result: Dict[str, Any], status: str, reason: str, flags: List[str]) -> Dict[str, Any]:
    agent_input = _build_agent_input(parse_result)
    candidates = agent_input.get("candidate_values", {}) or {}
    manual_override = agent_input.get("manual_override", {}) or {}
    ocr_reconciliation = agent_input.get("ocr_reconciliation", {}) or {}
    agreement_fields = (ocr_reconciliation.get("agreement_fields") or {}) if isinstance(ocr_reconciliation, dict) else {}
    recommended = {
        "historical_cost": None,
        "acquisition_year": None,
        "rendered_value": None,
        "good_faith_value": None,
        "attachment_total": None,
        "selected_source": None,
    }
    rejected: Dict[str, List[str]] = {
        "historical_cost": [],
        "acquisition_year": [],
        "rendered_value": [],
        "good_faith_value": [],
        "attachment_total": [],
    }
    review_flags = list(flags)
    selected_sources: Dict[str, str] = {}

    attachment_consensus = ((agreement_fields.get("attachment_total") or {}).get("value"))
    if attachment_consensus is not None:
        recommended["attachment_total"] = str(attachment_consensus)
        selected_sources["attachment_total"] = "ocr_provider_consensus"
        review_flags.append("ocr_provider_consensus_attachment_total")

    good_faith_consensus = ((agreement_fields.get("good_faith_total") or {}).get("value"))
    if good_faith_consensus is not None:
        recommended["good_faith_value"] = str(good_faith_consensus)
        selected_sources["good_faith_value"] = "ocr_provider_consensus"
        review_flags.append("ocr_provider_consensus_good_faith_total")

    for field in ["historical_cost", "acquisition_year", "rendered_value", "good_faith_value", "attachment_total"]:
        manual_value = _normalize_candidate(manual_override.get(field))
        if manual_value:
            recommended[field] = manual_value
            recommended["selected_source"] = "manual_override"
            continue

        if recommended.get(field) is not None:
            continue

        field_candidates = candidates.get(field, [])
        if not isinstance(field_candidates, list) or not field_candidates:
            review_flags.append(f"missing_{field}_candidate")
            continue

        sorted_candidates = sorted(
            [c for c in field_candidates if isinstance(c, dict) and _normalize_candidate(c.get("value"))],
            key=_candidate_sort_key,
            reverse=True,
        )
        if not sorted_candidates:
            review_flags.append(f"missing_{field}_candidate")
            continue

        best = sorted_candidates[0]
        best_value = _normalize_candidate(best.get("value"))
        recommended[field] = best_value
        selected_sources[field] = str(best.get("source") or best.get("rule") or field)

        for rejected_candidate in sorted_candidates[1:6]:
            value = _normalize_candidate(rejected_candidate.get("value"))
            if value and value != best_value:
                rejected[field].append(
                    f"{value} from {rejected_candidate.get('source', 'unknown')} page {rejected_candidate.get('page_number', '-')}"
                )

        if len({str(c.get("value")) for c in sorted_candidates[:3]}) > 1:
            review_flags.append(f"conflicting_{field}_candidates")
        if _candidate_sort_key(best) < 0.65:
            review_flags.append(f"low_confidence_{field}")

    if manual_override:
        review_flags.append("manual_override_authoritative")

    for preferred_field in ["attachment_total", "rendered_value", "good_faith_value", "historical_cost"]:
        if recommended.get(preferred_field):
            recommended["selected_source"] = selected_sources.get(preferred_field, preferred_field)
            break

    confidence = 0.45
    if recommended.get("attachment_total") or recommended.get("rendered_value") or recommended.get("good_faith_value"):
        confidence = 0.65
    if any(flag.startswith("low_confidence") or flag.startswith("conflicting") for flag in review_flags):
        confidence = min(confidence, 0.45)
    if manual_override:
        confidence = 0.95

    return {
        "status": status,
        "reason": reason,
        "recommended_values": recommended,
        "confidence": confidence,
        "reasoning": "Deterministic fallback review selected the highest-scored pipeline candidates and preserved manual overrides.",
        "review_flags": sorted(set(review_flags)),
        "rejected_candidates": rejected,
    }


def _build_agent_input(parse_result: Dict[str, Any]) -> Dict[str, Any]:
    attachments = _safe_get(parse_result, "attachments", {}) or {}
    form_flags = _safe_get(parse_result, "form_flags", {}) or {}
    manual_override = _safe_get(parse_result, "manual_override", {}) or {}
    page_summaries = _safe_get(parse_result, "page_summaries", []) or []
    page_texts = _safe_get(parse_result, "page_texts", []) or []
    ocr_reconciliation = _safe_get(parse_result, "ocr_reconciliation", {}) or {}
    structured_extraction = _safe_get(parse_result, "structured_extraction", {}) or {}
    schedule_breakdown = _safe_get(parse_result, "schedule_breakdown", {}) or {}
    metadata = _safe_get(parse_result, "metadata", {}) or {}
    review_flags = _safe_get(parse_result, "review_flags", {}) or {}

    # Keep page payload light. We do not want to dump massive text blobs unless they already exist and are needed.
    trimmed_pages = []
    if isinstance(page_summaries, list) and page_summaries:
        for item in page_summaries[:30]:
            if isinstance(item, dict):
                trimmed_pages.append(item)
    elif isinstance(page_texts, list) and page_texts:
        for item in page_texts[:20]:
            if isinstance(item, dict):
                trimmed_pages.append(
                    {
                        "page_number": item.get("page_number"),
                        "preview": str(item.get("text", ""))[:1200],
                    }
                )

    return {
        "processed_at": parse_result.get("processed_at"),
        "metadata": {
            "tax_year": metadata.get("tax_year"),
            "owner_name": metadata.get("owner_name"),
            "account_number": metadata.get("account_number"),
        },
        "form_flags": form_flags,
        "attachments": attachments,
        "manual_override": manual_override,
        "review_flags": review_flags,
        "ocr_reconciliation": {
            "chosen_provider": ocr_reconciliation.get("chosen_provider"),
            "secondary_providers": ocr_reconciliation.get("secondary_providers", []),
            "provider_agreement": bool(ocr_reconciliation.get("provider_agreement")),
            "provider_disagreement": bool(ocr_reconciliation.get("provider_disagreement")),
            "agreement_fields": ocr_reconciliation.get("agreement_fields", {}),
            "disagreement_fields": ocr_reconciliation.get("disagreement_fields", {}),
            "provider_summaries": ocr_reconciliation.get("provider_summaries", {}),
        },
        "structured_extraction": {
            "extraction_provider": structured_extraction.get("extraction_provider"),
            "document_confidence": structured_extraction.get("document_confidence"),
            "review_flags": structured_extraction.get("review_flags", []),
        },
        "schedule_breakdown": schedule_breakdown,
        "page_context": trimmed_pages,
        "candidate_values": _collect_top_candidates(parse_result),
    }


def review_parse_result(parse_result: Dict[str, Any]) -> Dict[str, Any]:
    """
    Reviews the current pipeline output with an AI reasoning layer.

    Returns a dict you can merge into your existing parse output:
    {
      "recommended_values": {...},
      "confidence": 0.0-1.0,
      "reasoning": "...",
      "review_flags": [...],
      "rejected_candidates": {...}
    }
    """
    if os.getenv("OPENAI_REVIEW_ENABLED", "").strip().lower() not in {"1", "true", "yes"}:
        return _fallback_review(
            parse_result,
            status="fallback",
            reason="OpenAI review disabled; deterministic review used",
            flags=["ai_review_disabled"],
        )

    api_key = _get_openai_api_key()
    if not api_key:
        return _fallback_review(
            parse_result,
            status="fallback",
            reason="OPENAI_API_KEY not set",
            flags=["ai_review_skipped_no_api_key"],
        )

    try:
        from openai import OpenAI
    except Exception as exc:
        return _fallback_review(
            parse_result,
            status="fallback",
            reason=f"OpenAI SDK unavailable: {exc}",
            flags=["ai_review_skipped_openai_sdk_missing"],
        )

    try:
        timeout_seconds = float(os.getenv("OPENAI_REVIEW_TIMEOUT_SECONDS", "8"))
    except ValueError:
        timeout_seconds = 8.0

    client = OpenAI(api_key=api_key, timeout=timeout_seconds, max_retries=0)
    agent_input = _build_agent_input(parse_result)

    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "recommended_values": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "historical_cost": {"type": ["string", "null"]},
                    "acquisition_year": {"type": ["string", "null"]},
                    "rendered_value": {"type": ["string", "null"]},
                    "good_faith_value": {"type": ["string", "null"]},
                    "attachment_total": {"type": ["string", "null"]},
                    "selected_source": {"type": ["string", "null"]},
                },
                "required": [
                    "historical_cost",
                    "acquisition_year",
                    "rendered_value",
                    "good_faith_value",
                    "attachment_total",
                    "selected_source",
                ],
            },
            "confidence": {"type": "number"},
            "reasoning": {"type": "string"},
            "review_flags": {
                "type": "array",
                "items": {"type": "string"},
            },
            "rejected_candidates": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "historical_cost": {"type": "array", "items": {"type": "string"}},
                    "acquisition_year": {"type": "array", "items": {"type": "string"}},
                    "rendered_value": {"type": "array", "items": {"type": "string"}},
                    "good_faith_value": {"type": "array", "items": {"type": "string"}},
                    "attachment_total": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "historical_cost",
                    "acquisition_year",
                    "rendered_value",
                    "good_faith_value",
                    "attachment_total",
                ],
            },
        },
        "required": [
            "recommended_values",
            "confidence",
            "reasoning",
            "review_flags",
            "rejected_candidates",
        ],
    }

    try:
        response = client.responses.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
            instructions=SYSTEM_PROMPT,
            input=json.dumps(agent_input, indent=2),
            text={
                "format": {
                    "type": "json_schema",
                    "name": "bpp_agent_review",
                    "schema": schema,
                    "strict": True,
                }
            },
        )
    except Exception as exc:
        return _fallback_review(
            parse_result,
            status="fallback",
            reason=f"AI review request failed: {type(exc).__name__}: {exc}",
            flags=["ai_review_request_error"],
        )

    try:
        result = json.loads(response.output_text)
    except Exception as exc:
        fallback = _fallback_review(
            parse_result,
            status="fallback",
            reason=f"Could not parse model output: {exc}",
            flags=["ai_review_parse_error"],
        )
        fallback["raw_output"] = getattr(response, "output_text", "")
        return fallback

    result = _guardrail_review(result, agent_input)
    result["status"] = "completed"
    return result
