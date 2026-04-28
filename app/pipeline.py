from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol

import requests

from app.assessment_summary import AssessmentSummaryBuilder
from app.candidate_extractor import CandidateExtractor
from app.extractor import PDFExtractor
from app.rendition_value_engine import calculate_rendition_value
from app.targeted_parser import TargetedRenditionParser

try:
    from app.agent_reviewer import review_parse_result
except Exception:
    from app.agent_review import review_parse_result


class PageType(str, Enum):
    MAIN_FORM = "main_form"
    ATTACHMENT = "attachment"
    SCHEDULE = "schedule"
    SIGNATURE = "signature"
    UNKNOWN = "unknown"


@dataclass
class PageClassification:
    page_number: int
    page_type: PageType
    confidence: float
    reasons: List[str] = field(default_factory=list)


@dataclass
class PageParseResult:
    page_number: int
    page_type: PageType
    parser_name: str
    fields: Dict[str, Any] = field(default_factory=dict)
    candidates: List[Dict[str, Any]] = field(default_factory=list)
    flags: Dict[str, Any] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)


@dataclass
class PipelineResult:
    classifications: List[PageClassification]
    page_results: List[PageParseResult]
    merged_fields: Dict[str, Any]
    merged_candidates: List[Dict[str, Any]]
    final_result: Dict[str, Any]


class ParserProtocol(Protocol):
    def parse_page(
        self,
        page_text: str,
        page_number: int,
        ocr_blocks: Optional[List[Dict[str, Any]]] = None,
    ) -> PageParseResult:
        ...


PAGE_TYPE_FIELD_BOOSTS = {
    ("good_faith_value", "main_form"): 0.10,
    ("historical_cost", "attachment"): 0.10,
    ("acquisition_year", "attachment"): 0.08,
    ("life_years", "attachment"): 0.08,
    ("attachment_total", "schedule"): 0.12,
}

PAGE_TYPE_FIELD_PENALTIES = {
    ("historical_cost", "main_form"): 0.15,
    ("historical_cost", "signature"): 0.25,
    ("good_faith_value", "attachment"): 0.10,
    ("good_faith_value", "signature"): 0.25,
    ("attachment_total", "main_form"): 0.08,
    ("attachment_total", "signature"): 0.25,
}

FIELD_ALIASES = {
    "historical_cost": [
        "historical cost",
        "original cost",
        "reported cost",
        "acquisition cost",
        "purchase cost",
        "cost new",
    ],
    "acquisition_year": [
        "year acquired",
        "date acquired",
        "acquisition year",
        "purchase year",
        "acquired",
    ],
    "rendered_value": [
        "rendered value",
        "value rendered",
        "total rendered value",
        "rendered market value",
    ],
    "good_faith_value": [
        "good faith estimate",
        "good faith value",
        "good faith",
        "market value",
        "owner's estimate",
        "owners estimate",
    ],
    "attachment_total": [
        "attachment total",
        "summary total",
        "schedule total",
        "total cost",
        "grand total",
        "total market value",
        "current value",
        "depreciated value",
        "total",
    ],
}

MONEY_RE = re.compile(
    r"""
    (?<![\w])
    \(?\$?\s*
    (?:
        \d{1,3}(?:[\s,]\d{3})+(?:\.\d{1,2})?
        |
        \d{2,9}(?:\.\d{1,2})?
    )
    \)?
    (?![\w])
    """,
    re.VERBOSE,
)

YEAR_RE = re.compile(r"\b(19[5-9]\d|20[0-4]\d)\b")

_OPENAI_VISION_OCR_DISABLED_REASON: Optional[str] = None
_AZURE_OCR_DISABLED_REASON: Optional[str] = None
_GOOGLE_VISION_OCR_DISABLED_REASON: Optional[str] = None


def _normalize_text(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"\s+", " ", (text or "")).strip()
    return text


def _normalize_ocrish_text(text: str) -> str:
    text = _normalize_text(text).lower()
    text = text.replace("|", "l")
    text = text.replace("\\", "")
    text = text.replace("/", "")
    text = text.replace("schidule", "schedule")
    text = text.replace("renditlon", "rendition")
    text = text.replace("rendilion", "rendition")
    text = text.replace("valuo", "value")
    text = text.replace("equipmenl", "equipment")
    return text


def _configure_tesseract() -> bool:
    try:
        import pytesseract
    except Exception:
        return False

    candidates = [
        shutil.which("tesseract"),
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        r"C:\Users\Brooks\AppData\Local\Programs\Tesseract-OCR\tesseract.exe",
    ]
    for exe_path in candidates:
        if exe_path and Path(exe_path).exists():
            pytesseract.pytesseract.tesseract_cmd = exe_path
            return True
    return False


def _get_config_value(*names: str) -> Optional[str]:
    for name in names:
        value = os.getenv(name)
        if value:
            return value.strip()

    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return None

    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            key, sep, value = line.partition("=")
            if sep and key.strip() in names:
                cleaned = value.strip().strip('"').strip("'")
                if cleaned:
                    return cleaned
    except Exception:
        return None

    return None


def _get_openai_api_key() -> Optional[str]:
    return _get_config_value("OPENAI_API_KEY")


def _parse_money(raw: str) -> Optional[float]:
    cleaned = _normalize_money_text(raw)
    try:
        value = float(cleaned)
    except ValueError:
        return None
    if value < 0:
        return None
    return value


def _normalize_money_text(raw: str) -> str:
    text = (
        str(raw or "")
        .replace("$", "")
        .replace("O", "0")
        .replace("o", "0")
        .strip("() ")
    )
    text = re.sub(r"\s+", " ", text)

    # OCR sometimes splits a leading digit off a comma-grouped amount:
    # "$ 1 84,724.43" should be "184,724.43".
    # Some OCR also renders thousands with dots: "1 50.606.17".
    text = re.sub(r"\b(\d)\s+(\d{2,3}(?:[,.]\d{3})+(?:[,.]\d{2})?)\b", r"\1\2", text)
    text = text.replace(" ", "")

    last_dot = text.rfind(".")
    last_comma = text.rfind(",")
    last_sep_idx = max(last_dot, last_comma)

    if last_sep_idx >= 0 and len(text) - last_sep_idx - 1 == 2:
        decimal_sep = text[last_sep_idx]
        thousands_sep = "," if decimal_sep == "." else "."
        integer_part = text[:last_sep_idx].replace(thousands_sep, "").replace(decimal_sep, "")
        decimal_part = text[last_sep_idx + 1:]
        return f"{integer_part}.{decimal_part}"

    if "," in text and "." in text:
        return text.replace(",", "").replace(".", "")

    if "," in text:
        if re.fullmatch(r"\d{1,3}(?:,\d{3})+", text):
            return text.replace(",", "")
        return text.replace(",", ".")

    if text.count(".") > 1:
        return text.replace(".", "")

    return text


def _money_candidates_with_spans(text: str) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    for match in MONEY_RE.finditer(text or ""):
        raw = match.group(0)
        value = _parse_money(raw)
        if value is None or not _is_plausible_money(value):
            continue
        candidates.append(
            {
                "value": value,
                "raw_value": raw.strip(),
                "start": match.start(),
                "end": match.end(),
            }
        )
    return candidates


def _is_plausible_money(value: float) -> bool:
    if value < 100:
        return False
    if value > 1_000_000_000:
        return False
    if float(value).is_integer() and 1900 <= int(value) <= 2049:
        return False
    return True


def _is_common_form_threshold(value: float) -> bool:
    return float(value) in {20000.0, 125000.0, 150000.0} and value != 150000.0


def _evidence_snippet(text: str, start: int, end: int, window: int = 90) -> str:
    snippet = (text or "")[max(0, start - window): min(len(text or ""), end + window)]
    return _normalize_text(snippet)


def _label_positions(text: str, labels: List[str]) -> List[tuple[int, str]]:
    lowered = _normalize_ocrish_text(text)
    positions: List[tuple[int, str]] = []
    for label in labels:
        idx = lowered.find(label)
        if idx >= 0:
            positions.append((idx, label))
    return positions


def _candidate_for_labeled_money(
    text: str,
    field: str,
    labels: List[str],
    page_number: int,
    source: str,
    base_confidence: float,
    window: int = 180,
) -> Optional[Dict[str, Any]]:
    money_candidates = _money_candidates_with_spans(text)
    if not money_candidates:
        return None

    label_hits = _label_positions(text, labels)
    if not label_hits:
        return None

    scored: List[tuple[float, Dict[str, Any], str, int]] = []
    norm_text = _normalize_ocrish_text(text)
    for label_pos, label in label_hits:
        for money in money_candidates:
            distance = money["start"] - label_pos
            if -40 <= distance <= window:
                score = base_confidence + max(0.0, (window - abs(distance)) / window) * 0.10
                snippet = norm_text[max(0, label_pos - 35): min(len(norm_text), money["end"] + 35)]
                if "total" in snippet:
                    score += 0.04
                if any(bad in snippet for bad in ["phone", "fax", "zip", "account", "page"]):
                    score -= 0.20
                if any(bad in snippet for bad in ["not more than", "or less", "under $20", "$20,000 or more"]):
                    score -= 0.60
                if money["value"] in {20000.0, 50000.0, 125000.0, 150000.0} and any(
                    bad in snippet for bad in ["not more than", "or less", "under", "or more", "tax code"]
                ):
                    continue
                scored.append((score, money, label, distance))

    if not scored:
        return None

    score, money, label, _distance = max(scored, key=lambda item: (item[0], item[1]["value"]))
    return {
        "field": field,
        "value": money["value"],
        "raw_value": money["raw_value"],
        "source": source,
        "rule": f"near_label:{label}",
        "page_number": page_number,
        "confidence": round(min(score, 0.98), 3),
        "evidence_text": _evidence_snippet(text, money["start"], money["end"]),
    }


def _year_candidate(
    text: str,
    field: str,
    labels: List[str],
    page_number: int,
    source: str,
    base_confidence: float,
    window: int = 120,
) -> Optional[Dict[str, Any]]:
    label_hits = _label_positions(text, labels)
    if not label_hits:
        return None

    scored: List[tuple[float, re.Match[str], str]] = []
    for label_pos, label in label_hits:
        for match in YEAR_RE.finditer(text or ""):
            distance = match.start() - label_pos
            if -30 <= distance <= window:
                score = base_confidence + max(0.0, (window - abs(distance)) / window) * 0.08
                scored.append((score, match, label))

    if not scored:
        return None

    score, match, label = max(scored, key=lambda item: item[0])
    return {
        "field": field,
        "value": int(match.group(1)),
        "raw_value": match.group(1),
        "source": source,
        "rule": f"near_label:{label}",
        "page_number": page_number,
        "confidence": round(min(score, 0.96), 3),
        "evidence_text": _evidence_snippet(text, match.start(), match.end()),
    }


def _extract_first_year(patterns: List[str], text: str) -> Optional[int]:
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            try:
                year = int(match.group(1))
                if 1900 <= year <= 2100:
                    return year
            except ValueError:
                continue
    return None


def _extract_first_int(patterns: List[str], text: str, low: int, high: int) -> Optional[int]:
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            try:
                value = int(match.group(1))
                if low <= value <= high:
                    return value
            except ValueError:
                continue
    return None


def _ocr_blocks_to_text(ocr_blocks: Optional[List[Dict[str, Any]]]) -> str:
    if not ocr_blocks:
        return ""

    parts: List[str] = []
    for block in ocr_blocks:
        if isinstance(block, dict):
            txt = block.get("text", "")
            if txt:
                parts.append(str(txt))
        elif isinstance(block, str):
            parts.append(block)
    return " ".join(parts).strip()


def _find_money_candidates(text: str) -> List[float]:
    return [item["value"] for item in _money_candidates_with_spans(text)]


def _extract_labeled_money(text: str, labels: List[str], window: int = 120) -> Optional[float]:
    lowered = text.lower()

    for label in labels:
        idx = lowered.find(label.lower())
        if idx == -1:
            continue

        snippet = text[idx: idx + window]
        values = _find_money_candidates(snippet)

        filtered = [
            v for v in values
            if v >= 100  # kill Jan 1 / line numbers / tiny junk
        ]
        if filtered:
            return filtered[0]

    return None


def _extract_best_total_money(text: str) -> Optional[float]:
    values = _find_money_candidates(text)
    filtered = [v for v in values if v >= 100]
    if not filtered:
        return None
    return max(filtered)


def _garbage_table_score(text: str) -> float:
    if not text:
        return 0.0

    raw = text
    letters = sum(ch.isalpha() for ch in raw)
    digits = sum(ch.isdigit() for ch in raw)
    punct = sum((not ch.isalnum() and not ch.isspace()) for ch in raw)

    length = max(len(raw), 1)
    punct_ratio = punct / length
    digit_ratio = digits / length

    score = 0.0
    if length > 80:
        score += 0.05
    if punct_ratio > 0.08:
        score += 0.08
    if digit_ratio > 0.02:
        score += 0.04
    if letters > 20 and punct > 10:
        score += 0.05

    return score


class PageClassifier:
    def classify(self, page_text: str, page_number: int) -> PageClassification:
        text = _normalize_text(page_text)
        norm = _normalize_ocrish_text(page_text)
        reasons: List[str] = []

        scores: Dict[PageType, float] = {
            PageType.MAIN_FORM: 0.0,
            PageType.ATTACHMENT: 0.0,
            PageType.SCHEDULE: 0.0,
            PageType.SIGNATURE: 0.0,
            PageType.UNKNOWN: 0.0,
        }

        main_form_terms = [
            ("business personal property", 0.35, "found 'business personal property'"),
            ("rendition", 0.25, "found 'rendition'"),
            ("general information", 0.15, "found 'general information'"),
            ("good faith estimate", 0.15, "found 'good faith estimate'"),
            ("property owner", 0.06, "found 'property owner'"),
            ("account number", 0.06, "found 'account number'"),
            ("mailing address", 0.05, "found 'mailing address'"),
            ("confidential", 0.04, "found 'confidential'"),
        ]
        for term, weight, reason in main_form_terms:
            if term in norm:
                scores[PageType.MAIN_FORM] += weight
                reasons.append(reason)

        attachment_terms = [
            ("see attached", 0.30, "found 'see attached'"),
            ("attachment", 0.20, "found 'attachment'"),
            ("historical cost", 0.25, "found historical cost"),
            ("original cost", 0.25, "found original cost"),
            ("year acquired", 0.15, "found year acquired"),
            ("acquired", 0.10, "found acquired"),
            ("useful life", 0.10, "found useful life"),
        ]
        for term, weight, reason in attachment_terms:
            if term in norm:
                scores[PageType.ATTACHMENT] += weight
                reasons.append(reason)

        schedule_terms = [
            ("schedule", 0.18, "found schedule"),
            ("schidule", 0.18, "found schidule"),
            ("schedul", 0.14, "found schedul"),
            ("furniture", 0.16, "found furniture"),
            ("fixtures", 0.16, "found fixtures"),
            ("machinery", 0.16, "found machinery"),
            ("equipment", 0.16, "found equipment"),
            ("computers", 0.12, "found computers"),
            ("quantity", 0.15, "found quantity"),
            ("qty", 0.15, "found qty"),
            ("description", 0.12, "found description"),
            ("year acquired", 0.10, "found year acquired"),
            ("total cost", 0.15, "found total cost"),
            ("grand total", 0.15, "found grand total"),
            ("total(by year", 0.14, "found total by year"),
            ("totalby year", 0.14, "found totalby year"),
            ("by year acqu", 0.12, "found by year acquired fragment"),
        ]
        for term, weight, reason in schedule_terms:
            if term in norm:
                scores[PageType.SCHEDULE] += weight
                reasons.append(reason)

        if "description" in norm and "cost" in norm:
            scores[PageType.SCHEDULE] += 0.20
            reasons.append("found description + cost pattern")
        if "furniture" in norm and "fixtures" in norm:
            scores[PageType.SCHEDULE] += 0.12
            reasons.append("found furniture + fixtures pattern")
        if "machinery" in norm and "equipment" in norm:
            scores[PageType.SCHEDULE] += 0.12
            reasons.append("found machinery + equipment pattern")

        signature_terms = [
            ("signature", 0.30, "found 'signature'"),
            ("authorized representative", 0.20, "found authorized representative"),
            ("date signed", 0.15, "found date signed"),
            ("sworn and subscribed", 0.30, "found sworn and subscribed"),
            ("notary", 0.12, "found notary"),
            ("printed name", 0.06, "found printed name"),
        ]
        for term, weight, reason in signature_terms:
            if term in norm:
                scores[PageType.SIGNATURE] += weight
                reasons.append(reason)

        table_score = _garbage_table_score(text)
        if table_score > 0:
            scores[PageType.SCHEDULE] += table_score
            reasons.append("table/continuation page heuristic")

        best_type = max(scores, key=scores.get)
        best_score = scores[best_type]

        if best_score < 0.15:
            best_type = PageType.UNKNOWN
            reasons.append("no strong page-type match")

        return PageClassification(
            page_number=page_number,
            page_type=best_type,
            confidence=round(best_score, 3),
            reasons=reasons,
        )


class MainFormParser:
    def parse_page(
        self,
        page_text: str,
        page_number: int,
        ocr_blocks: Optional[List[Dict[str, Any]]] = None,
    ) -> PageParseResult:
        candidates: List[Dict[str, Any]] = []
        flags: Dict[str, Any] = {}

        combined_text = _normalize_text(f"{page_text} {_ocr_blocks_to_text(ocr_blocks)}")
        lowered = combined_text.lower()

        candidate = _candidate_for_labeled_money(
            combined_text,
            field="good_faith_value",
            labels=FIELD_ALIASES["good_faith_value"],
            page_number=page_number,
            source="main_form",
            base_confidence=0.78,
            window=140,
        )

        if candidate is not None and ("good faith" in lowered or "market value" in lowered):
            candidates.append(candidate)

        rendered = _candidate_for_labeled_money(
            combined_text,
            field="rendered_value",
            labels=FIELD_ALIASES["rendered_value"],
            page_number=page_number,
            source="main_form",
            base_confidence=0.80,
            window=160,
        )
        if rendered is not None:
            candidates.append(rendered)

        if "see attached" in lowered:
            flags["see_attached"] = True

        if "signature" in lowered:
            flags["signature_block_detected"] = True

        return PageParseResult(
            page_number=page_number,
            page_type=PageType.MAIN_FORM,
            parser_name=self.__class__.__name__,
            candidates=candidates,
            flags=flags,
        )


class AttachmentParser:
    def parse_page(
        self,
        page_text: str,
        page_number: int,
        ocr_blocks: Optional[List[Dict[str, Any]]] = None,
    ) -> PageParseResult:
        candidates: List[Dict[str, Any]] = []
        flags: Dict[str, Any] = {"attachment_detected": True}

        combined_text = _normalize_text(f"{page_text} {_ocr_blocks_to_text(ocr_blocks)}")
        lowered = combined_text.lower()

        historical_cost = _candidate_for_labeled_money(
            combined_text,
            field="historical_cost",
            labels=FIELD_ALIASES["historical_cost"],
            page_number=page_number,
            source="attachment",
            base_confidence=0.84,
            window=180,
        )
        if historical_cost is not None and ("cost" in lowered or "historical" in lowered or "original" in lowered):
            candidates.append(historical_cost)

        acquisition_year = _year_candidate(
            combined_text,
            field="acquisition_year",
            labels=FIELD_ALIASES["acquisition_year"],
            page_number=page_number,
            source="attachment",
            base_confidence=0.74,
        )
        if acquisition_year is not None and ("acquired" in lowered or "year acquired" in lowered):
            candidates.append(acquisition_year)

        rendered = _candidate_for_labeled_money(
            combined_text,
            field="rendered_value",
            labels=FIELD_ALIASES["rendered_value"],
            page_number=page_number,
            source="attachment",
            base_confidence=0.82,
            window=180,
        )
        if rendered is not None:
            candidates.append(rendered)

        life_years = _extract_first_int(
            [
                r"(?:useful life|life)\D{0,20}(\d{1,2})",
            ],
            combined_text,
            low=1,
            high=50,
        )
        if life_years is not None and ("life" in lowered or "useful life" in lowered):
            candidates.append({
                "field": "life_years",
                "value": life_years,
                "source": "attachment",
                "page_number": page_number,
                "confidence": 0.72,
            })

        return PageParseResult(
            page_number=page_number,
            page_type=PageType.ATTACHMENT,
            parser_name=self.__class__.__name__,
            candidates=candidates,
            flags=flags,
        )


class ScheduleParser:
    def parse_page(
        self,
        page_text: str,
        page_number: int,
        ocr_blocks: Optional[List[Dict[str, Any]]] = None,
    ) -> PageParseResult:
        candidates: List[Dict[str, Any]] = []
        flags: Dict[str, Any] = {"schedule_detected": True}

        combined_text = _normalize_text(f"{page_text} {_ocr_blocks_to_text(ocr_blocks)}")
        lowered = combined_text.lower()

        total_candidate = _candidate_for_labeled_money(
            combined_text,
            field="attachment_total",
            labels=FIELD_ALIASES["attachment_total"],
            page_number=page_number,
            source="schedule",
            base_confidence=0.82,
            window=220,
        )

        if total_candidate is None and any(term in lowered for term in ["schedule", "schidule", "fixtures", "machinery", "equipment"]):
            # Do not infer a schedule total from the largest row value on Schedule E-style
            # detail tables. Those pages often contain year-by-year costs where the largest
            # handwritten row is not the section total.
            if "schedule e" in lowered or "furniture" in lowered or "computer equipment" in lowered:
                total_value = None
            else:
                total_value = _extract_best_total_money(combined_text)
            if total_value is not None:
                total_candidate = {
                    "field": "attachment_total",
                    "value": total_value,
                    "source": "schedule",
                    "rule": "largest_plausible_schedule_amount",
                    "page_number": page_number,
                    "confidence": 0.68,
                    "evidence_text": combined_text[:250],
                }

        if total_candidate is not None:
            candidates.append(total_candidate)

        rendered = _candidate_for_labeled_money(
            combined_text,
            field="rendered_value",
            labels=FIELD_ALIASES["rendered_value"],
            page_number=page_number,
            source="schedule",
            base_confidence=0.80,
            window=180,
        )
        if rendered is not None:
            candidates.append(rendered)

        if "furniture" in lowered:
            flags["schedule_contains_furniture"] = True
        if "fixtures" in lowered:
            flags["schedule_contains_fixtures"] = True
        if "machinery" in lowered:
            flags["schedule_contains_machinery"] = True
        if "equipment" in lowered:
            flags["schedule_contains_equipment"] = True

        return PageParseResult(
            page_number=page_number,
            page_type=PageType.SCHEDULE,
            parser_name=self.__class__.__name__,
            candidates=candidates,
            flags=flags,
        )


class SignatureParser:
    def parse_page(
        self,
        page_text: str,
        page_number: int,
        ocr_blocks: Optional[List[Dict[str, Any]]] = None,
    ) -> PageParseResult:
        combined_text = _normalize_text(f"{page_text} {_ocr_blocks_to_text(ocr_blocks)}")
        flags = {
            "signature_block_detected": "signature" in combined_text.lower()
        }

        return PageParseResult(
            page_number=page_number,
            page_type=PageType.SIGNATURE,
            parser_name=self.__class__.__name__,
            candidates=[],
            flags=flags,
        )


class UnknownPageParser:
    def parse_page(
        self,
        page_text: str,
        page_number: int,
        ocr_blocks: Optional[List[Dict[str, Any]]] = None,
    ) -> PageParseResult:
        return PageParseResult(
            page_number=page_number,
            page_type=PageType.UNKNOWN,
            parser_name=self.__class__.__name__,
            candidates=[],
            flags={"unknown_page": True},
        )


class CandidateScorer:
    def score(self, candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
        best_by_field: Dict[str, Dict[str, Any]] = {}

        for candidate in candidates:
            field = candidate.get("field")
            if not field:
                continue

            base_conf = float(candidate.get("confidence", 0))
            source = str(candidate.get("source", "")).lower()

            boost = PAGE_TYPE_FIELD_BOOSTS.get((field, source), 0.0)
            penalty = PAGE_TYPE_FIELD_PENALTIES.get((field, source), 0.0)

            candidate["score"] = max(0.0, base_conf + boost - penalty)

            current = best_by_field.get(field)
            if current is None or candidate["score"] > current.get("score", 0):
                best_by_field[field] = candidate

        return {
            "best_candidates": best_by_field,
            "resolved_values": {k: v.get("value") for k, v in best_by_field.items()},
        }


def bucket_candidates(candidates: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    buckets: Dict[str, List[Dict[str, Any]]] = {
        "historical_cost": [],
        "acquisition_year": [],
        "rendered_value": [],
        "good_faith_value": [],
        "attachment_total": [],
    }
    for candidate in candidates:
        field_name = candidate.get("field") or candidate.get("label")
        if field_name in buckets:
            buckets[field_name].append(candidate)
    for field_candidates in buckets.values():
        field_candidates.sort(
            key=lambda item: float(item.get("score", item.get("confidence", 0)) or 0),
            reverse=True,
        )
    return buckets


class Pipeline:
    def __init__(self) -> None:
        self.classifier = PageClassifier()
        self.candidate_scorer = CandidateScorer()

        self.parser_registry: Dict[PageType, ParserProtocol] = {
            PageType.MAIN_FORM: MainFormParser(),
            PageType.ATTACHMENT: AttachmentParser(),
            PageType.SCHEDULE: ScheduleParser(),
            PageType.SIGNATURE: SignatureParser(),
            PageType.UNKNOWN: UnknownPageParser(),
        }

    def _post_process_classifications(
        self,
        pages: List[Dict[str, Any]],
        classifications: List[PageClassification],
    ) -> List[PageClassification]:
        if not classifications:
            return classifications

        adjusted = list(classifications)

        for idx in range(1, len(adjusted)):
            prev_cls = adjusted[idx - 1]
            curr_cls = adjusted[idx]
            curr_text = _normalize_ocrish_text(pages[idx].get("text", ""))

            if (
                prev_cls.page_type == PageType.SCHEDULE
                and curr_cls.page_type == PageType.UNKNOWN
                and len(curr_text) > 60
            ):
                adjusted[idx] = PageClassification(
                    page_number=curr_cls.page_number,
                    page_type=PageType.SCHEDULE,
                    confidence=0.22,
                    reasons=curr_cls.reasons + ["inherited schedule continuation from previous page"],
                )

        return adjusted

    def run(
        self,
        pages: List[Dict[str, Any]],
        manual_override: Optional[Dict[str, Any]] = None,
    ) -> PipelineResult:
        raw_classifications: List[PageClassification] = []

        for page in pages:
            raw_classifications.append(
                self.classifier.classify(
                    page_text=page.get("text", "") or "",
                    page_number=int(page["page_number"]),
                )
            )

        classifications = self._post_process_classifications(pages, raw_classifications)

        page_results: List[PageParseResult] = []
        merged_candidates: List[Dict[str, Any]] = []
        merged_fields: Dict[str, Any] = {
            "form_flags": {},
            "attachments": {},
        }

        for page, classification in zip(pages, classifications):
            page_number = int(page["page_number"])
            page_text = page.get("text", "") or ""
            ocr_blocks = page.get("ocr_blocks", [])

            parser = self.parser_registry.get(
                classification.page_type,
                self.parser_registry[PageType.UNKNOWN],
            )

            try:
                result = parser.parse_page(
                    page_text=page_text,
                    page_number=page_number,
                    ocr_blocks=ocr_blocks,
                )
            except Exception as exc:
                result = PageParseResult(
                    page_number=page_number,
                    page_type=classification.page_type,
                    parser_name=parser.__class__.__name__,
                    candidates=[],
                    flags={},
                    errors=[f"{type(exc).__name__}: {exc}"],
                )

            page_results.append(result)
            merged_candidates.extend(result.candidates)

            for k, v in result.fields.items():
                merged_fields[k] = v

            for k, v in result.flags.items():
                merged_fields["form_flags"][k] = v

        scoring_output = self.candidate_scorer.score(merged_candidates)
        candidate_buckets = bucket_candidates(merged_candidates)

        resolved_values = scoring_output.get("resolved_values", {})
        if manual_override:
            resolved_values = self._apply_manual_overrides(resolved_values, manual_override)

        final_result = {
            "processed_pages": len(pages),
            "page_classifications": [
                {
                    "page_number": c.page_number,
                    "page_type": c.page_type.value,
                    "confidence": c.confidence,
                    "reasons": c.reasons,
                }
                for c in classifications
            ],
            "merged_candidates": merged_candidates,
            "candidates": candidate_buckets,
            "scoring": scoring_output,
            "resolved_values": resolved_values,
            "manual_override": manual_override or {},
            "form_flags": merged_fields.get("form_flags", {}),
            "page_summaries": [
                {
                    "page_number": r.page_number,
                    "page_type": r.page_type.value,
                    "parser": r.parser_name,
                    "candidate_count": len(r.candidates),
                    "flags": r.flags,
                }
                for r in page_results
            ],
        }

        return PipelineResult(
            classifications=classifications,
            page_results=page_results,
            merged_fields=merged_fields,
            merged_candidates=merged_candidates,
            final_result=final_result,
        )

    def _apply_manual_overrides(
        self,
        resolved_values: Dict[str, Any],
        manual_override: Dict[str, Any],
    ) -> Dict[str, Any]:
        merged = dict(resolved_values)
        for key, value in manual_override.items():
            if value is not None:
                merged[key] = value
        return merged


def load_pages_from_json(json_path: str) -> List[Dict[str, Any]]:
    path = Path(json_path)
    if not path.exists():
        raise FileNotFoundError(f"JSON file not found: {json_path}")

    data = json.loads(path.read_text(encoding="utf-8"))

    if isinstance(data, dict):
        if "pages" in data and isinstance(data["pages"], list):
            data = data["pages"]
        elif "document_pages" in data and isinstance(data["document_pages"], list):
            data = data["document_pages"]
        else:
            raise ValueError("JSON dict must contain 'pages' or 'document_pages' list.")

    if not isinstance(data, list):
        raise ValueError("Expected a list of pages in JSON.")

    pages: List[Dict[str, Any]] = []
    for i, page in enumerate(data, start=1):
        if not isinstance(page, dict):
            raise ValueError(f"Page entry {i} is not a dict.")

        text = (
            page.get("text")
            or page.get("page_text")
            or page.get("content")
            or ""
        )

        ocr_blocks = (
            page.get("ocr_blocks")
            or page.get("blocks")
            or page.get("ocr")
            or []
        )

        pages.append({
            "page_number": page.get("page_number", page.get("page", i)),
            "text": text,
            "ocr_blocks": ocr_blocks,
        })

    return pages


def load_pages_from_txt_folder(folder_path: str) -> List[Dict[str, Any]]:
    folder = Path(folder_path)
    if not folder.exists():
        raise FileNotFoundError(f"Folder not found: {folder_path}")
    if not folder.is_dir():
        raise ValueError(f"Not a folder: {folder_path}")

    txt_files = sorted(folder.glob("*.txt"))
    if not txt_files:
        raise ValueError(f"No .txt files found in folder: {folder_path}")

    pages: List[Dict[str, Any]] = []
    for i, txt_file in enumerate(txt_files, start=1):
        raw_text = txt_file.read_text(encoding="utf-8", errors="ignore")
        text = _normalize_text(raw_text)

        pages.append({
            "page_number": i,
            "text": text,
            "ocr_blocks": [],
            "source_file": str(txt_file),
        })

    return pages


def get_demo_pages() -> List[Dict[str, Any]]:
    return [
        {
            "page_number": 1,
            "text": "Business Personal Property Rendition General Information Good Faith Estimate 12000",
            "ocr_blocks": [],
        },
        {
            "page_number": 2,
            "text": "Attachment Historical Cost 4500 Year Acquired 2022 Life 5",
            "ocr_blocks": [],
        },
        {
            "page_number": 3,
            "text": "SCHIDULE E Furniture Fixtures Machinery Equipment Computers Total(by year acquired) Grand Total 25000",
            "ocr_blocks": [],
        },
        {
            "page_number": 4,
            "text": "3:P != 6JE 96 gh E6 5ii continuation table page totals rows cost quantities",
            "ocr_blocks": [],
        },
    ]


def print_loaded_page_preview(pages: List[Dict[str, Any]], preview_chars: int = 300) -> None:
    print("\nLOADED PAGE PREVIEW")
    print("-" * 50)
    for page in pages:
        text = page.get("text", "") or ""
        preview = text[:preview_chars]
        preview = preview.replace("\n", " ").replace("\r", " ")
        source_file = page.get("source_file", "")
        print(f"Page {page.get('page_number')}: chars={len(text)}")
        if source_file:
            print(f"  source_file: {source_file}")
        print(f"  preview: {preview if preview else '[EMPTY PAGE TEXT]'}")


def print_pipeline_debug(result: PipelineResult) -> None:
    print("\nPAGE CLASSIFICATIONS")
    print("-" * 50)
    for c in result.classifications:
        print(f"Page {c.page_number}: {c.page_type.value} (confidence={c.confidence})")
        if c.reasons:
            print(f"  reasons: {', '.join(c.reasons)}")

    print("\nPAGE PARSE RESULTS")
    print("-" * 50)
    for r in result.page_results:
        print(
            f"Page {r.page_number}: parser={r.parser_name}, "
            f"candidates={len(r.candidates)}, errors={r.errors}"
        )
        for cand in r.candidates:
            print(f"  - {cand}")

    print("\nFINAL RESOLVED VALUES")
    print("-" * 50)
    for k, v in result.final_result.get("resolved_values", {}).items():
        print(f"{k}: {v}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Page classification + parser routing pipeline")
    parser.add_argument(
        "--pages-json",
        type=str,
        default="",
        help="Path to JSON file containing page text/OCR blocks.",
    )
    parser.add_argument(
        "--pages-txt-folder",
        type=str,
        default="",
        help="Folder of page .txt files, one file per page.",
    )
    parser.add_argument(
        "--manual-override-json",
        type=str,
        default="",
        help="Optional JSON file containing manual overrides.",
    )
    return parser


def load_manual_override(path_str: str) -> Dict[str, Any]:
    if not path_str:
        return {}

    path = Path(path_str)
    if not path.exists():
        raise FileNotFoundError(f"Manual override JSON not found: {path_str}")

    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Manual override JSON must be a dict/object.")

    return data


def _provider_has_text(pages: List[Dict[str, Any]]) -> bool:
    return any((page.get("text") or "").strip() for page in pages)


def _provider_text_chars(pages: List[Dict[str, Any]]) -> int:
    return sum(len(page.get("text", "") or "") for page in pages)


def _provider_value_summary(pages: List[Dict[str, Any]]) -> Dict[str, Any]:
    targeted_parser = TargetedRenditionParser()
    schedule_e = _best_schedule_e(pages, targeted_parser)
    attachments = targeted_parser.parse_attachment_summary([p.get("text", "") or "" for p in pages])
    schedule_values = _extract_schedule_values(pages)
    return {
        "page_count": len(pages),
        "text_chars": _provider_text_chars(pages),
        "text_source": next((p.get("text_source") for p in pages if p.get("text_source")), None),
        "schedule_e_total": schedule_e.get("total"),
        "attachment_total": attachments.get("best_attachment_total"),
        "good_faith_total": schedule_values.get("good_faith_total"),
    }


def _values_agree(left: Any, right: Any, tolerance: float = 1.0) -> bool:
    if left is None or right is None:
        return False
    try:
        return abs(float(left) - float(right)) <= tolerance
    except (TypeError, ValueError):
        return False


OCR_PROVIDER_PRIORITY = [
    "google_cloud_vision",
    "openai_vision_ocr",
    "azure_document_intelligence",
    "pymupdf_tesseract_ocr",
    "embedded_pdf_text",
]


def _reconcile_ocr_providers(
    embedded_pages: List[Dict[str, Any]],
    provider_pages: Dict[str, List[Dict[str, Any]]],
    chosen_provider: str,
) -> Dict[str, Any]:
    provider_summaries: Dict[str, Dict[str, Any]] = {}
    if _provider_has_text(embedded_pages):
        provider_summaries["embedded_pdf_text"] = _provider_value_summary(embedded_pages)

    for provider_name, pages in provider_pages.items():
        if _provider_has_text(pages):
            provider_summaries[provider_name] = _provider_value_summary(pages)

    agreement_fields: Dict[str, Dict[str, Any]] = {}
    disagreement_fields: Dict[str, Dict[str, float]] = {}
    candidate_fields = ["attachment_total", "schedule_e_total", "good_faith_total"]

    for field_name in candidate_fields:
        values = {
            provider_name: summary.get(field_name)
            for provider_name, summary in provider_summaries.items()
            if summary.get(field_name) is not None
        }
        if len(values) < 2:
            continue

        numeric_values = [float(value) for value in values.values()]
        if max(numeric_values) - min(numeric_values) <= 1.0:
            agreement_fields[field_name] = {
                "value": round(sum(numeric_values) / len(numeric_values), 2),
                "providers": sorted(values.keys()),
            }
        else:
            disagreement_fields[field_name] = {
                provider_name: float(value)
                for provider_name, value in values.items()
            }

    preferred_summary = provider_summaries.get(chosen_provider, {})
    secondary_providers = [
        provider_name
        for provider_name in OCR_PROVIDER_PRIORITY
        if provider_name != chosen_provider and provider_name in provider_summaries
    ]

    return {
        "used_fallback_ocr": chosen_provider != "embedded_pdf_text",
        "chosen_provider": chosen_provider,
        "secondary_providers": secondary_providers,
        "provider_summaries": provider_summaries,
        "provider_agreement": bool(agreement_fields),
        "provider_disagreement": bool(disagreement_fields),
        "agreement_fields": agreement_fields,
        "disagreement_fields": disagreement_fields,
        "chosen_provider_summary": preferred_summary,
    }


def _extract_pdf_bundle(pdf_path: str) -> Dict[str, Any]:
    started_at = time.perf_counter()
    extractor = PDFExtractor()
    embedded_pages: List[Dict[str, Any]] = []
    embedded_extraction_error: Optional[str] = None
    try:
        embedded_pages = extractor.extract_pages(pdf_path)
    except Exception as exc:
        embedded_extraction_error = (
            f"Embedded PDF text extraction failed: {type(exc).__name__}: {exc}"
        )

    for page in embedded_pages:
        page_number = int(page.get("page_number", 1))
        try:
            words = extractor.extract_page_words(pdf_path, page_number)
        except Exception:
            words = []
        normalized_words = []
        for word in words:
            if not isinstance(word, dict):
                continue
            normalized_words.append(
                {
                    **word,
                    "top": word.get("top", word.get("y0", 0)),
                    "y0": word.get("y0", word.get("top", 0)),
                }
            )
        page["ocr_blocks"] = normalized_words
        page.setdefault("text_source", "embedded_pdf_text")

    if embedded_pages and not _needs_ocr_fallback(embedded_pages):
        reconciliation = _reconcile_ocr_providers(embedded_pages, {}, "embedded_pdf_text")
        for page in embedded_pages:
            page["extraction_provider"] = "embedded_pdf_text"
            page["extraction_seconds"] = round(time.perf_counter() - started_at, 2)
        return {
            "pages": embedded_pages,
            "ocr_reconciliation": reconciliation,
        }

    provider_pages = {
        "google_cloud_vision": _ocr_pdf_pages_with_google_vision(pdf_path),
        "openai_vision_ocr": _ocr_pdf_pages_with_openai_vision(pdf_path),
        "azure_document_intelligence": _ocr_pdf_pages_with_azure_document_intelligence(pdf_path),
        "pymupdf_tesseract_ocr": _ocr_pdf_pages_with_pymupdf(pdf_path),
    }

    chosen_provider = next(
        (provider_name for provider_name in OCR_PROVIDER_PRIORITY if _provider_has_text(provider_pages.get(provider_name, []))),
        "embedded_pdf_text",
    )
    chosen_pages = provider_pages.get(chosen_provider) or embedded_pages
    reconciliation = _reconcile_ocr_providers(embedded_pages, provider_pages, chosen_provider)

    provider_errors = []
    if embedded_extraction_error:
        provider_errors.append(embedded_extraction_error)
    for provider_name in ["google_cloud_vision", "openai_vision_ocr", "azure_document_intelligence", "pymupdf_tesseract_ocr"]:
        provider_errors.extend(
            str(page.get("ocr_error"))
            for page in provider_pages.get(provider_name, [])
            if page.get("ocr_error")
        )

    if not chosen_pages:
        chosen_pages = [
            {
                "page_number": 1,
                "text": "",
                "ocr_blocks": [],
                "text_source": "pdf_extraction_error" if embedded_extraction_error else "embedded_pdf_text",
            }
        ]

    if chosen_provider == "embedded_pdf_text" and (
        embedded_extraction_error or _needs_ocr_fallback(embedded_pages)
    ):
        for page in chosen_pages:
            page["ocr_unavailable"] = True
            if provider_errors:
                page["ocr_error"] = _summarize_ocr_error(provider_errors[0])

    for page in chosen_pages:
        page["extraction_provider"] = chosen_provider
        page["extraction_seconds"] = round(time.perf_counter() - started_at, 2)

    return {
        "pages": chosen_pages,
        "ocr_reconciliation": reconciliation,
    }


def _extract_pdf_pages(pdf_path: str) -> List[Dict[str, Any]]:
    return _extract_pdf_bundle(pdf_path)["pages"]


def _needs_ocr_fallback(pages: List[Dict[str, Any]]) -> bool:
    if not pages:
        return False

    combined = "\n".join((p.get("text") or "") for p in pages)
    normalized = _normalize_ocrish_text(combined)
    raw_tokens = re.findall(r"\b\S+\b", combined)
    text_chars = len(combined.strip())
    alpha_chars = sum(ch.isalpha() for ch in combined)
    digit_chars = sum(ch.isdigit() for ch in combined)
    page_count = max(len(pages), 1)

    if text_chars < 75 * page_count:
        return True
    if alpha_chars < 25 * page_count and digit_chars < 10 * page_count:
        return True

    target_terms = [
        "rendition",
        "schedule",
        "market value",
        "good faith",
        "historical cost",
        "property owner",
        "total fixed assets",
    ]
    target_term_hits = sum(1 for term in target_terms if term in normalized)

    suspicious_token_count = sum(
        1
        for token in raw_tokens
        if (
            len(token) >= 4
            and re.search(r"[A-Za-z]", token)
            and re.search(r"\d", token)
        )
        or "\\" in token
        or "€" in token
    )
    if suspicious_token_count >= max(12, 6 * page_count):
        return True

    return target_term_hits < 2


def _ocr_pdf_pages_with_pymupdf(pdf_path: str) -> List[Dict[str, Any]]:
    try:
        import fitz
        import pytesseract
        from PIL import Image
        from pytesseract import Output
    except Exception:
        return []

    if not _configure_tesseract():
        return []

    try:
        doc = fitz.open(pdf_path)
    except Exception:
        return []

    ocr_pages: List[Dict[str, Any]] = []
    try:
        for page_number, page in enumerate(doc, start=1):
            pix = page.get_pixmap(matrix=fitz.Matrix(4, 4), alpha=False)
            image = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            image = _prepare_ocr_image(image)

            best_text = ""
            best_words: List[Dict[str, Any]] = []
            best_score = -1

            for rotation in [0, 90, 270]:
                rotated = image.rotate(rotation, expand=True) if rotation else image
                try:
                    config = "--oem 3 --psm 6 -c preserve_interword_spaces=1"
                    text = pytesseract.image_to_string(rotated, config=config) or ""
                    data = pytesseract.image_to_data(rotated, output_type=Output.DICT, config=config)
                except Exception:
                    continue

                norm = _normalize_ocrish_text(text)
                score = len(text)
                for term in ["schedule", "good faith", "historical cost", "rendition", "property owner"]:
                    if term in norm:
                        score += 500

                words: List[Dict[str, Any]] = []
                for idx, word_text in enumerate(data.get("text", [])):
                    cleaned = str(word_text or "").strip()
                    if not cleaned:
                        continue
                    try:
                        conf = float(data.get("conf", ["-1"])[idx])
                    except Exception:
                        conf = -1
                    if conf < 0:
                        continue
                    left = float(data.get("left", [0])[idx])
                    top = float(data.get("top", [0])[idx])
                    width = float(data.get("width", [0])[idx])
                    height = float(data.get("height", [0])[idx])
                    words.append(
                        {
                            "text": cleaned,
                            "x0": left,
                            "x1": left + width,
                            "top": top,
                            "y0": top,
                            "y1": top + height,
                            "confidence": conf,
                            "rotation": rotation,
                        }
                    )

                if score > best_score:
                    best_score = score
                    best_text = text
                    best_words = words

            ocr_pages.append(
                {
                    "page_number": page_number,
                    "text": best_text,
                    "ocr_blocks": best_words,
                    "text_source": "pymupdf_tesseract_ocr",
                }
            )
    finally:
        doc.close()

    return ocr_pages


def _prepare_ocr_image(image: Any) -> Any:
    try:
        from PIL import ImageEnhance, ImageFilter
    except Exception:
        return image

    gray = image.convert("L")
    gray = ImageEnhance.Contrast(gray).enhance(1.8)
    gray = gray.filter(ImageFilter.SHARPEN)
    return gray.point(lambda px: 255 if px > 185 else 0)


def _summarize_ocr_error(error: str) -> str:
    lowered = error.lower()
    if "google cloud vision" in lowered or "google vision" in lowered:
        if "not configured" in lowered:
            return "Google Cloud Vision OCR unavailable: API key not configured."
        if "quota" in lowered or "billing" in lowered or "resource_exhausted" in lowered:
            return "Google Cloud Vision OCR unavailable: quota or billing limit reached."
        if "permission denied" in lowered or "403" in lowered:
            return "Google Cloud Vision OCR unavailable: API key or service permissions failed."
        if "unauthenticated" in lowered or "401" in lowered or "api key not valid" in lowered:
            return "Google Cloud Vision OCR unavailable: API key authentication failed."
        if "timed out" in lowered or "timeout" in lowered:
            return "Google Cloud Vision OCR unavailable: timed out."
        return "Google Cloud Vision OCR unavailable."
    if "azure document intelligence" in lowered:
        if "not configured" in lowered:
            return "Azure Document Intelligence OCR unavailable: endpoint/key not configured."
        if "quota" in lowered or "billing" in lowered:
            return "Azure Document Intelligence OCR unavailable: quota or billing limit reached."
        if "unauthorized" in lowered or "forbidden" in lowered or "401" in lowered or "403" in lowered:
            return "Azure Document Intelligence OCR unavailable: endpoint/key authentication failed."
        if "timed out" in lowered or "timeout" in lowered:
            return "Azure Document Intelligence OCR unavailable: timed out."
        return "Azure Document Intelligence OCR unavailable."
    if "insufficient_quota" in lowered or "exceeded your current quota" in lowered:
        return "OpenAI vision OCR unavailable: API quota or billing limit exceeded."
    if "rate" in lowered and "limit" in lowered:
        return "OpenAI vision OCR unavailable: rate limit reached."
    if "api key" in lowered or "authentication" in lowered or "unauthorized" in lowered:
        return "OpenAI vision OCR unavailable: API key/authentication failed."
    return error


def _is_terminal_google_ocr_error(error: str) -> bool:
    lowered = error.lower()
    return any(
        marker in lowered
        for marker in [
            "api key not valid",
            "permission denied",
            "billing",
            "quota",
            "resource_exhausted",
            "access not configured",
            "service disabled",
            "project",
        ]
    )


def _ocr_pdf_pages_with_google_vision(pdf_path: str) -> List[Dict[str, Any]]:
    global _GOOGLE_VISION_OCR_DISABLED_REASON

    if _GOOGLE_VISION_OCR_DISABLED_REASON:
        return [
            {
                "page_number": 1,
                "text": "",
                "ocr_blocks": [],
                "text_source": "google_cloud_vision_error",
                "ocr_error": _GOOGLE_VISION_OCR_DISABLED_REASON,
            }
        ]

    api_key = _get_config_value("GOOGLE_VISION_API_KEY", "GOOGLE_CLOUD_VISION_API_KEY")
    if not api_key:
        return []

    try:
        import fitz
    except Exception:
        return []

    try:
        dpi_scale = float(_get_config_value("GOOGLE_VISION_OCR_DPI_SCALE") or "3.0")
    except ValueError:
        dpi_scale = 3.0
    try:
        request_timeout = float(_get_config_value("GOOGLE_VISION_OCR_REQUEST_TIMEOUT_SECONDS") or "20")
    except ValueError:
        request_timeout = 20.0
    try:
        max_pages = int(_get_config_value("GOOGLE_VISION_OCR_MAX_PAGES") or "0")
    except ValueError:
        max_pages = 0

    endpoint = "https://vision.googleapis.com/v1/images:annotate"

    try:
        doc = fitz.open(pdf_path)
    except Exception:
        return []

    pages: List[Dict[str, Any]] = []
    try:
        for page_number, page in enumerate(doc, start=1):
            if max_pages > 0 and page_number > max_pages:
                break

            pix = page.get_pixmap(matrix=fitz.Matrix(dpi_scale, dpi_scale), alpha=False)
            image_b64 = base64.b64encode(pix.tobytes("jpeg", jpg_quality=90)).decode("ascii")
            payload = {
                "requests": [
                    {
                        "image": {"content": image_b64},
                        "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
                    }
                ]
            }

            try:
                response = requests.post(
                    endpoint,
                    params={"key": api_key},
                    json=payload,
                    timeout=request_timeout,
                )
            except requests.RequestException as exc:
                error = f"Google Cloud Vision request failed: {type(exc).__name__}: {exc}"
                return [{"page_number": page_number, "text": "", "ocr_blocks": [], "text_source": "google_cloud_vision_error", "ocr_error": error}]

            if response.status_code >= 400:
                error = f"Google Cloud Vision HTTP {response.status_code}: {response.text[:500]}"
                if _is_terminal_google_ocr_error(error):
                    _GOOGLE_VISION_OCR_DISABLED_REASON = error
                return [{"page_number": page_number, "text": "", "ocr_blocks": [], "text_source": "google_cloud_vision_error", "ocr_error": error}]

            try:
                response_payload = response.json()
            except ValueError as exc:
                error = f"Google Cloud Vision returned non-JSON response: {type(exc).__name__}: {exc}"
                return [{"page_number": page_number, "text": "", "ocr_blocks": [], "text_source": "google_cloud_vision_error", "ocr_error": error}]

            result = ((response_payload.get("responses") or [{}])[0]) if isinstance(response_payload, dict) else {}
            if result.get("error"):
                error = f"Google Cloud Vision API error: {json.dumps(result.get('error'))[:500]}"
                if _is_terminal_google_ocr_error(error):
                    _GOOGLE_VISION_OCR_DISABLED_REASON = error
                return [{"page_number": page_number, "text": "", "ocr_blocks": [], "text_source": "google_cloud_vision_error", "ocr_error": error}]

            text = str((result.get("fullTextAnnotation") or {}).get("text") or "").strip()
            pages.append(
                {
                    "page_number": page_number,
                    "text": text,
                    "ocr_blocks": _google_vision_response_to_word_blocks(result),
                    "text_source": "google_cloud_vision",
                }
            )
    finally:
        doc.close()

    return pages


def _google_vision_response_to_word_blocks(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    word_blocks: List[Dict[str, Any]] = []
    annotation = result.get("fullTextAnnotation") or {}
    pages = annotation.get("pages") or []

    for page in pages:
        for block in page.get("blocks") or []:
            for paragraph in block.get("paragraphs") or []:
                for word in paragraph.get("words") or []:
                    symbols = word.get("symbols") or []
                    text = "".join(str(symbol.get("text") or "") for symbol in symbols).strip()
                    if not text:
                        continue
                    vertices = (word.get("boundingBox") or {}).get("vertices") or []
                    xs = [float(vertex.get("x", 0) or 0) for vertex in vertices]
                    ys = [float(vertex.get("y", 0) or 0) for vertex in vertices]
                    confidence = word.get("confidence")
                    word_blocks.append(
                        {
                            "text": text,
                            "x0": min(xs) if xs else 0,
                            "x1": max(xs) if xs else 0,
                            "top": min(ys) if ys else 0,
                            "y0": min(ys) if ys else 0,
                            "y1": max(ys) if ys else 0,
                            "confidence": confidence,
                            "source": "google_cloud_vision",
                        }
                    )

    word_blocks.sort(key=lambda item: (round(float(item.get("top", 0)), 1), round(float(item.get("x0", 0)), 1)))
    return word_blocks


def _is_terminal_azure_ocr_error(error: str) -> bool:
    lowered = error.lower()
    return any(
        marker in lowered
        for marker in [
            "not configured",
            "quota",
            "billing",
            "401",
            "403",
            "unauthorized",
            "forbidden",
            "invalid subscription",
            "resource not found",
        ]
    )


def _ocr_pdf_pages_with_azure_document_intelligence(pdf_path: str) -> List[Dict[str, Any]]:
    global _AZURE_OCR_DISABLED_REASON

    if _AZURE_OCR_DISABLED_REASON:
        return [
            {
                "page_number": 1,
                "text": "",
                "ocr_blocks": [],
                "text_source": "azure_document_intelligence_error",
                "ocr_error": _AZURE_OCR_DISABLED_REASON,
            }
        ]

    endpoint = _get_config_value(
        "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT",
        "AZURE_FORM_RECOGNIZER_ENDPOINT",
    )
    key = _get_config_value(
        "AZURE_DOCUMENT_INTELLIGENCE_KEY",
        "AZURE_FORM_RECOGNIZER_KEY",
    )
    if not endpoint or not key:
        return []

    endpoint = endpoint.rstrip("/")
    api_version = _get_config_value("AZURE_DOCUMENT_INTELLIGENCE_API_VERSION") or "2023-07-31"
    model_id = _get_config_value("AZURE_DOCUMENT_INTELLIGENCE_MODEL_ID") or "prebuilt-read"
    if api_version.startswith("2024"):
        analyze_url = f"{endpoint}/documentintelligence/documentModels/{model_id}:analyze?api-version={api_version}"
    else:
        analyze_url = f"{endpoint}/formrecognizer/documentModels/{model_id}:analyze?api-version={api_version}"

    try:
        total_timeout = float(_get_config_value("AZURE_DOCUMENT_INTELLIGENCE_TIMEOUT_SECONDS") or "18")
    except ValueError:
        total_timeout = 18.0
    try:
        request_timeout = float(_get_config_value("AZURE_DOCUMENT_INTELLIGENCE_REQUEST_TIMEOUT_SECONDS") or "8")
    except ValueError:
        request_timeout = 8.0

    headers = {
        "Ocp-Apim-Subscription-Key": key,
        "Content-Type": "application/pdf",
    }

    try:
        pdf_bytes = Path(pdf_path).read_bytes()
    except Exception as exc:
        return [
            {
                "page_number": 1,
                "text": "",
                "ocr_blocks": [],
                "text_source": "azure_document_intelligence_error",
                "ocr_error": f"Azure Document Intelligence file read failed: {type(exc).__name__}: {exc}",
            }
        ]

    started = time.perf_counter()
    try:
        response = requests.post(
            analyze_url,
            headers=headers,
            data=pdf_bytes,
            timeout=request_timeout,
        )
    except requests.RequestException as exc:
        error = f"Azure Document Intelligence request failed: {type(exc).__name__}: {exc}"
        return [{"page_number": 1, "text": "", "ocr_blocks": [], "text_source": "azure_document_intelligence_error", "ocr_error": error}]

    if response.status_code not in {200, 202}:
        error = f"Azure Document Intelligence HTTP {response.status_code}: {response.text[:500]}"
        if _is_terminal_azure_ocr_error(error):
            _AZURE_OCR_DISABLED_REASON = error
        return [{"page_number": 1, "text": "", "ocr_blocks": [], "text_source": "azure_document_intelligence_error", "ocr_error": error}]

    operation_location = response.headers.get("operation-location") or response.headers.get("Operation-Location")
    if response.status_code == 200:
        result_payload = response.json()
    elif operation_location:
        result_payload = None
        while time.perf_counter() - started < total_timeout:
            try:
                poll = requests.get(
                    operation_location,
                    headers={"Ocp-Apim-Subscription-Key": key},
                    timeout=request_timeout,
                )
            except requests.RequestException as exc:
                error = f"Azure Document Intelligence poll failed: {type(exc).__name__}: {exc}"
                return [{"page_number": 1, "text": "", "ocr_blocks": [], "text_source": "azure_document_intelligence_error", "ocr_error": error}]

            if poll.status_code >= 400:
                error = f"Azure Document Intelligence poll HTTP {poll.status_code}: {poll.text[:500]}"
                if _is_terminal_azure_ocr_error(error):
                    _AZURE_OCR_DISABLED_REASON = error
                return [{"page_number": 1, "text": "", "ocr_blocks": [], "text_source": "azure_document_intelligence_error", "ocr_error": error}]

            result_payload = poll.json()
            status = str(result_payload.get("status", "")).lower()
            if status == "succeeded":
                break
            if status == "failed":
                error = f"Azure Document Intelligence analysis failed: {json.dumps(result_payload)[:500]}"
                return [{"page_number": 1, "text": "", "ocr_blocks": [], "text_source": "azure_document_intelligence_error", "ocr_error": error}]
            time.sleep(1.0)
        else:
            return [
                {
                    "page_number": 1,
                    "text": "",
                    "ocr_blocks": [],
                    "text_source": "azure_document_intelligence_error",
                    "ocr_error": "Azure Document Intelligence timed out.",
                }
            ]
    else:
        return [
            {
                "page_number": 1,
                "text": "",
                "ocr_blocks": [],
                "text_source": "azure_document_intelligence_error",
                "ocr_error": "Azure Document Intelligence did not return an operation-location header.",
            }
        ]

    return _azure_analyze_result_to_pages(result_payload or {})


def _azure_analyze_result_to_pages(result_payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    analyze_result = result_payload.get("analyzeResult") or result_payload
    raw_pages = analyze_result.get("pages") or []
    pages: List[Dict[str, Any]] = []

    if raw_pages:
        for raw_page in raw_pages:
            page_number = int(raw_page.get("pageNumber") or len(pages) + 1)
            lines = raw_page.get("lines") or []
            words = raw_page.get("words") or []
            line_text = "\n".join(str(line.get("content") or "") for line in lines if line.get("content"))
            word_blocks = []
            for word in words:
                xs, ys = _polygon_xy(word.get("polygon") or word.get("boundingPolygon") or [])
                word_blocks.append(
                    {
                        "text": str(word.get("content") or ""),
                        "x0": min(xs) if xs else 0,
                        "x1": max(xs) if xs else 0,
                        "top": min(ys) if ys else 0,
                        "y0": min(ys) if ys else 0,
                        "y1": max(ys) if ys else 0,
                        "confidence": word.get("confidence"),
                        "source": "azure_document_intelligence",
                    }
                )
            pages.append(
                {
                    "page_number": page_number,
                    "text": line_text,
                    "ocr_blocks": word_blocks,
                    "text_source": "azure_document_intelligence",
                }
            )

    if pages:
        return pages

    content = str(analyze_result.get("content") or "").strip()
    if content:
        return [
            {
                "page_number": 1,
                "text": content,
                "ocr_blocks": [],
                "text_source": "azure_document_intelligence",
            }
        ]
    return []


def _polygon_xy(polygon: Any) -> tuple[List[float], List[float]]:
    if not isinstance(polygon, list):
        return [], []
    if polygon and all(isinstance(point, dict) for point in polygon):
        xs = [float(point.get("x", 0)) for point in polygon]
        ys = [float(point.get("y", 0)) for point in polygon]
        return xs, ys
    if polygon and all(isinstance(point, (int, float)) for point in polygon):
        xs = [float(polygon[idx]) for idx in range(0, len(polygon), 2)]
        ys = [float(polygon[idx]) for idx in range(1, len(polygon), 2)]
        return xs, ys
    return [], []


def _is_terminal_openai_ocr_error(error: str) -> bool:
    lowered = error.lower()
    return any(
        marker in lowered
        for marker in [
            "insufficient_quota",
            "exceeded your current quota",
            "api key",
            "authentication",
            "unauthorized",
            "billing",
        ]
    )


def _ocr_pdf_pages_with_openai_vision(pdf_path: str) -> List[Dict[str, Any]]:
    global _OPENAI_VISION_OCR_DISABLED_REASON

    if _OPENAI_VISION_OCR_DISABLED_REASON:
        return [
            {
                "page_number": 1,
                "text": "",
                "ocr_blocks": [],
                "text_source": "openai_vision_ocr_error",
                "ocr_error": _OPENAI_VISION_OCR_DISABLED_REASON,
            }
        ]

    api_key = _get_openai_api_key()
    if not api_key:
        return []

    try:
        import fitz
        from openai import OpenAI
    except Exception:
        return []

    model = os.getenv("OPENAI_VISION_OCR_MODEL") or os.getenv("OPENAI_MODEL") or "gpt-4.1-mini"

    try:
        doc = fitz.open(pdf_path)
    except Exception:
        return []

    try:
        timeout_seconds = float(os.getenv("OPENAI_VISION_OCR_TIMEOUT_SECONDS", "12"))
    except ValueError:
        timeout_seconds = 12.0

    client = OpenAI(api_key=api_key, timeout=timeout_seconds, max_retries=0)
    rendered_pages: List[tuple[int, str]] = []
    try:
        max_pages = int(os.getenv("OPENAI_VISION_OCR_MAX_PAGES", "8"))
    except ValueError:
        max_pages = 8

    try:
        for page_number, page in enumerate(doc, start=1):
            if page_number > max_pages:
                break
            try:
                pix = page.get_pixmap(matrix=fitz.Matrix(2.5, 2.5), alpha=False)
                image_bytes = pix.tobytes("jpeg", jpg_quality=88)
                image_url = "data:image/jpeg;base64," + base64.b64encode(image_bytes).decode("ascii")
            except Exception:
                continue
            rendered_pages.append((page_number, image_url))
    finally:
        doc.close()

    if not rendered_pages:
        return []

    content: List[Dict[str, Any]] = [
        {
            "type": "input_text",
            "text": (
                "Transcribe these business personal property rendition pages. "
                "Return visible text only. Preserve line breaks and dollar amounts exactly. "
                "Do not summarize or infer. Separate each page with a line exactly like PAGE 1:, PAGE 2:, etc."
            ),
        }
    ]
    for page_number, image_url in rendered_pages:
        content.append({"type": "input_text", "text": f"PAGE {page_number}:"})
        content.append({"type": "input_image", "image_url": image_url})

    try:
        response = client.responses.create(
            model=model,
            input=[
                {
                    "role": "user",
                    "content": content,
                }
            ],
        )
        combined_text = (getattr(response, "output_text", "") or "").strip()
    except Exception as exc:
        error_text = f"{type(exc).__name__}: {exc}"
        if _is_terminal_openai_ocr_error(error_text):
            _OPENAI_VISION_OCR_DISABLED_REASON = error_text
        return [
            {
                "page_number": rendered_pages[0][0],
                "text": "",
                "ocr_blocks": [],
                "text_source": "openai_vision_ocr_error",
                "ocr_error": error_text,
            }
        ]

    page_texts = _split_openai_vision_pages(combined_text)
    pages: List[Dict[str, Any]] = []
    for page_number, _image_url in rendered_pages:
        pages.append(
            {
                "page_number": page_number,
                "text": page_texts.get(page_number, ""),
                "ocr_blocks": [],
                "text_source": "openai_vision_ocr",
            }
        )

    if not any((page.get("text") or "").strip() for page in pages) and combined_text:
        pages = [
            {
                "page_number": 1,
                "text": combined_text,
                "ocr_blocks": [],
                "text_source": "openai_vision_ocr",
            }
        ]

    return pages


def _split_openai_vision_pages(text: str) -> Dict[int, str]:
    matches = list(re.finditer(r"(?im)^\s*PAGE\s+(\d+)\s*:\s*$", text or ""))
    if not matches:
        return {1: text.strip()} if text.strip() else {}

    pages: Dict[int, str] = {}
    for idx, match in enumerate(matches):
        page_number = int(match.group(1))
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        pages[page_number] = text[start:end].strip()
    return pages


def _best_schedule_e(pages: List[Dict[str, Any]], targeted_parser: TargetedRenditionParser) -> Dict[str, Any]:
    best = {
        "schedule_e_present": False,
        "machinery_and_equipment_present": False,
        "total": None,
        "page_number": None,
        "year_rows": [],
        "subsection_rows": [],
        "subsection_totals": {},
    }
    for page in pages:
        page_number = int(page.get("page_number", 1))
        text_result = targeted_parser.parse_schedule_e_total(page.get("text", "") or "")
        rows = targeted_parser.parse_schedule_e_year_rows_from_words(page.get("ocr_blocks", []) or [])
        subsection_rows = targeted_parser.parse_schedule_e_subsection_rows(page.get("ocr_blocks", []) or [])
        subsection_totals = targeted_parser.parse_schedule_e_subsection_totals(page.get("ocr_blocks", []) or [])

        if text_result.get("schedule_e_present"):
            best["schedule_e_present"] = True
        if text_result.get("machinery_and_equipment_present"):
            best["machinery_and_equipment_present"] = True
        if rows:
            best["year_rows"].extend(rows)
        if subsection_rows:
            best["subsection_rows"].extend(
                [{**row, "source_page": page_number} for row in subsection_rows]
            )
        if subsection_totals:
            best["subsection_totals"].update(subsection_totals)

        total = text_result.get("schedule_e_total")
        if (
            text_result.get("schedule_e_present")
            and total is not None
            and (best["total"] is None or float(total) > float(best["total"]))
        ):
            best["total"] = total
            best["page_number"] = page_number

        if text_result.get("schedule_e_present") and best["total"] is None and subsection_totals:
            computed_total = round(sum(float(value or 0) for value in subsection_totals.values()), 2)
            if computed_total > 0:
                best["total"] = computed_total
                best["page_number"] = page_number

        if text_result.get("schedule_e_present") and best["total"] is None and rows:
            computed_total = round(sum(float(row.get("amount") or 0) for row in rows), 2)
            if computed_total > 0:
                best["total"] = computed_total
                best["page_number"] = page_number

    return best


def _find_schedule_sections(text: str) -> Dict[str, str]:
    matches = list(re.finditer(r"\bSCHEDULE\s+([A-F])\b", text or "", re.IGNORECASE))
    sections: Dict[str, str] = {}
    for idx, match in enumerate(matches):
        letter = match.group(1).upper()
        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        sections[letter] = text[start:end]
    return sections


def _find_explicit_dollar_values(text: str) -> List[float]:
    values: List[float] = []
    for match in re.finditer(r"\$\s*([0-9Ool]{1,3}(?:[,\s.][0-9Ool]{3})*|[0-9Ool]{2,9})(?:\.\d{1,2})?", text or ""):
        value = _parse_money(match.group(0))
        if value is None or not _is_plausible_money(value):
            continue
        if value in {20000.0, 125000.0}:
            continue
        values.append(value)
    return values


def _extract_schedule_values(pages: List[Dict[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "schedules": {},
        "candidates": [],
        "good_faith_total": None,
        "historical_cost_total": None,
    }

    for page in pages:
        page_number = int(page.get("page_number", 1))
        page_text = page.get("text", "") or ""
        sections = _find_schedule_sections(page_text)
        if not sections:
            continue

        for letter, section_text in sections.items():
            dollar_values = _find_explicit_dollar_values(section_text)
            section_entry = result["schedules"].setdefault(
                letter,
                {
                    "schedule": letter,
                    "page_numbers": [],
                    "dollar_values": [],
                    "good_faith_values": [],
                    "historical_cost_values": [],
                    "total": None,
                },
            )
            section_entry["page_numbers"].append(page_number)
            section_entry["dollar_values"].extend(dollar_values)

            norm_section = _normalize_ocrish_text(section_text)
            if letter == "A" and dollar_values and "good faith" in norm_section:
                section_entry["good_faith_values"].extend(dollar_values)
                total = round(sum(dollar_values), 2)
                section_entry["total"] = total
                result["good_faith_total"] = round((result["good_faith_total"] or 0) + total, 2)
                result["candidates"].append(
                    {
                        "field": "good_faith_value",
                        "value": total,
                        "raw_value": ", ".join(f"${v:,.2f}" for v in dollar_values),
                        "source": "schedule_a",
                        "rule": "sum_schedule_a_good_faith_estimates",
                        "page_number": page_number,
                        "confidence": 0.90,
                        "score": 1.02,
                        "evidence_text": _normalize_text(section_text[:500]),
                    }
                )
            elif dollar_values:
                total = round(sum(dollar_values), 2)
                section_entry["total"] = total
                result["candidates"].append(
                    {
                        "field": "attachment_total",
                        "value": total,
                        "raw_value": ", ".join(f"${v:,.2f}" for v in dollar_values),
                        "source": f"schedule_{letter.lower()}",
                        "rule": f"sum_schedule_{letter.lower()}_explicit_dollar_values",
                        "page_number": page_number,
                        "confidence": 0.78,
                        "score": 0.90,
                        "evidence_text": _normalize_text(section_text[:500]),
                    }
                )

            if "historical cost" in norm_section:
                historical_values = dollar_values
                section_entry["historical_cost_values"].extend(historical_values)
                if historical_values:
                    result["historical_cost_total"] = round(
                        (result["historical_cost_total"] or 0) + sum(historical_values),
                        2,
                    )

    for section_entry in result["schedules"].values():
        section_entry["page_numbers"] = sorted(set(section_entry["page_numbers"]))
        section_entry["dollar_values"] = sorted(section_entry["dollar_values"], reverse=True)
        section_entry["good_faith_values"] = sorted(section_entry["good_faith_values"], reverse=True)
        section_entry["historical_cost_values"] = sorted(section_entry["historical_cost_values"], reverse=True)

    return result


def _review_flags(
    pages: List[Dict[str, Any]],
    schedule_e: Dict[str, Any],
    attachments: Dict[str, Any],
    ocr_reconciliation: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    text_chars = sum(len(p.get("text", "") or "") for p in pages)
    ocr_errors = sorted(
        {
            str(p.get("ocr_error"))
            for p in pages
            if p.get("ocr_error")
        }
    )
    ocr_reconciliation = ocr_reconciliation or {}
    return {
        "needs_manual_row_review": bool(schedule_e.get("schedule_e_present") and not schedule_e.get("year_rows")),
        "needs_attachment_review": bool(
            attachments.get("attachment_summary_present")
            and attachments.get("best_attachment_total") is None
        ),
        "low_text_extraction": text_chars < 50,
        "ocr_unavailable": any(bool(p.get("ocr_unavailable")) for p in pages),
        "ocr_errors": ocr_errors,
        "ocr_provider_used": ocr_reconciliation.get("chosen_provider"),
        "ocr_secondary_providers": ocr_reconciliation.get("secondary_providers", []),
        "provider_agreement": bool(ocr_reconciliation.get("provider_agreement")),
        "provider_disagreement": bool(ocr_reconciliation.get("provider_disagreement")),
        "provider_agreement_fields": sorted((ocr_reconciliation.get("agreement_fields") or {}).keys()),
    }


def _extract_metadata(pages: List[Dict[str, Any]]) -> Dict[str, Any]:
    first_text = pages[0].get("text", "") if pages else ""
    combined = "\n".join((page.get("text", "") or "") for page in pages[:2])
    word_text = " ".join(
        str(word.get("text", ""))
        for page in pages[:2]
        for word in (page.get("ocr_blocks", []) or [])
        if isinstance(word, dict)
    )
    search_text = f"{combined}\n{word_text}"

    def first_match(patterns: List[str], text: str) -> Optional[str]:
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                value = _normalize_text(match.group(1))
                if value:
                    return value
        return None

    tax_year = first_match(
        [
            r"\b(20[0-4]\d)\s+(?:business personal property|rendition)",
            r"(?:tax year|year)\D{0,15}(20[0-4]\d)",
        ],
        first_text,
    )
    owner_name = first_match(
        [
            r"(?:property owner|owner name|name of owner)\s*[:\-]?\s*([A-Z0-9&., '\-]{3,80})",
            r"(?:owner)\s*[:\-]?\s*([A-Z0-9&., '\-]{3,80})",
        ],
        combined,
    )
    account_number = first_match(
        [
            r"\b(P\s*[-#]?\s*\d{4,10})\b",
            r"(?:account number|account no\.?|acct\.?|property id|pid)\s*[:#\-]?\s*([A-Z0-9\-]{4,30})",
            r"(?:appraisal district account|account)\D{0,30}(P\s*[-#]?\s*\d{4,10})",
            r"\b(?:account|acct)\D{0,10}([0-9]{4,30})\b",
        ],
        search_text,
    )
    if account_number:
        account_number = re.sub(r"[^A-Z0-9]", "", account_number.upper())
    signed_date = first_match(
        [
            r"(?:date signed|signed date|date)\s*[:\-]?\s*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
        ],
        combined,
    )

    return {
        "tax_year": tax_year,
        "owner_name": owner_name,
        "account_number": account_number,
        "signed_date": signed_date,
    }


def _build_page_texts(pages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "page_number": page.get("page_number"),
            "text": page.get("text", "") or "",
        }
        for page in pages
    ]


def _dedupe_candidates(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: set[tuple[str, str, int, str]] = set()
    deduped: List[Dict[str, Any]] = []
    for candidate in sorted(
        candidates,
        key=lambda item: float(item.get("score", item.get("confidence", 0)) or 0),
        reverse=True,
    ):
        field_name = str(candidate.get("field") or candidate.get("label") or "")
        value = str(candidate.get("value"))
        page_number = int(candidate.get("page_number") or 0)
        source = str(candidate.get("source") or "")
        key = (field_name, value, page_number, source)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped


def _depreciate_manual_override(manual_override: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    manual_override = manual_override or {}
    historical_cost = manual_override.get("historical_cost")
    acquisition_year = manual_override.get("acquisition_year")
    life_years = manual_override.get("life_years")

    if historical_cost is None:
        return {}

    schedule_path = Path(__file__).resolve().parent.parent / "Data" / "depreciation_schedule.csv"
    if not schedule_path.exists():
        return {
            "percent_good": None,
            "depreciated_value": None,
            "error": f"Depreciation schedule not found: {schedule_path}",
        }

    try:
        from app.depreciation import DepreciationEngine

        engine = DepreciationEngine(str(schedule_path))
        pct, value = engine.assess_value(
            original_cost=float(historical_cost),
            life_years=int(life_years) if life_years is not None else None,
            acquisition_year=int(acquisition_year) if acquisition_year is not None else None,
        )
        return {"percent_good": pct, "depreciated_value": value}
    except Exception as exc:
        return {"percent_good": None, "depreciated_value": None, "error": f"{type(exc).__name__}: {exc}"}


def run_rendition_pipeline(
    pdf_path: str,
    manual_override: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    extraction_bundle = _extract_pdf_bundle(pdf_path)
    pages = extraction_bundle.get("pages", [])
    ocr_reconciliation = extraction_bundle.get("ocr_reconciliation", {}) or {}
    targeted_parser = TargetedRenditionParser()

    form_flags = targeted_parser.parse_page_1_flags(pages[0].get("text", "") if pages else "")
    schedule_e = _best_schedule_e(pages, targeted_parser)
    schedule_values = _extract_schedule_values(pages)
    attachments = targeted_parser.parse_attachment_summary([p.get("text", "") or "" for p in pages])
    metadata = _extract_metadata(pages)

    pipeline = Pipeline()
    pipeline_result = pipeline.run(pages=pages, manual_override=manual_override)

    value_candidates = CandidateExtractor().extract_candidates(pages)
    attachment_total_candidate: List[Dict[str, Any]] = []
    if attachments.get("best_attachment_total") is not None:
        attachment_total_candidate.append(
            {
                "field": "attachment_total",
                "value": attachments.get("best_attachment_total"),
                "raw_value": str(attachments.get("best_attachment_total")),
                "source": "attachment_summary",
                "rule": "labeled_attachment_total",
                "page_number": None,
                "confidence": 0.99,
                "score": 100.0,
                "evidence_text": "Labeled attachment total selected from attachment summary.",
            }
        )
    merged_candidates = _dedupe_candidates(
        (pipeline_result.final_result.get("merged_candidates", []) or [])
        + value_candidates
        + attachment_total_candidate
        + (schedule_values.get("candidates", []) or [])
    )
    candidate_buckets = bucket_candidates(merged_candidates)
    selected_candidate = CandidateExtractor().select_best_candidate(value_candidates) or {}

    review_flags = _review_flags(pages, schedule_e, attachments, ocr_reconciliation)
    depreciated_override_result = _depreciate_manual_override(manual_override)

    result = dict(pipeline_result.final_result)
    valuation_result = calculate_rendition_value(
        {
            **result,
            "pages": pages,
            "metadata": metadata,
            "schedule_e": schedule_e,
            "schedule_values": schedule_values,
            "review_flags": review_flags,
        }
    )
    schedule_breakdown = {
        "schedule_a_total": (valuation_result.get("schedule_totals", {}) or {}).get("A", 0.0),
        "schedule_b_total": (valuation_result.get("schedule_totals", {}) or {}).get("B", 0.0),
        "schedule_c_total": (valuation_result.get("schedule_totals", {}) or {}).get("C", 0.0),
        "schedule_d_total": (valuation_result.get("schedule_totals", {}) or {}).get("D", 0.0),
        "schedule_e_total": (valuation_result.get("schedule_totals", {}) or {}).get("E", 0.0),
    }
    schedule_e_breakdown = {
        "furniture_fixtures": (valuation_result.get("subsection_totals", {}) or {}).get("furniture_fixtures", 0.0),
        "machinery_equipment": (valuation_result.get("subsection_totals", {}) or {}).get("machinery_equipment", 0.0),
        "office_equipment": (valuation_result.get("subsection_totals", {}) or {}).get("office_equipment", 0.0),
        "computer_equipment": (valuation_result.get("subsection_totals", {}) or {}).get("computer_equipment", 0.0),
        "pos_servers_mainframes": (valuation_result.get("subsection_totals", {}) or {}).get("pos_servers_mainframes", 0.0),
        "other": (valuation_result.get("subsection_totals", {}) or {}).get("other", 0.0),
    }
    result.update(
        {
            "source_pdf": str(pdf_path),
            "processed_at": datetime.now().isoformat(timespec="seconds"),
            "processed_pages": len(pages),
            "page_texts": _build_page_texts(pages),
            "form_flags": {**(result.get("form_flags", {}) or {}), **form_flags},
            "schedule_e": schedule_e,
            "schedule_values": schedule_values,
            "attachments": attachments,
            "metadata": metadata,
            "review_flags": review_flags,
            "ocr_reconciliation": ocr_reconciliation,
            "manual_override": manual_override or {},
            "depreciated_override_result": depreciated_override_result,
            "value_candidates": value_candidates,
            "selected_candidate": selected_candidate,
            "merged_candidates": merged_candidates,
            "candidates": candidate_buckets,
            "rendition_valuation": valuation_result,
            "recommended_value": valuation_result.get("final_recommended_value"),
            "recommended_value_source": "schedule_rule_engine" if valuation_result.get("final_recommended_value") is not None else None,
            "schedule_breakdown": schedule_breakdown,
            "schedule_e_breakdown": schedule_e_breakdown,
            "valuation_flags": valuation_result.get("flags", []),
            "extracted_line_items": valuation_result.get("line_items", []),
        }
    )

    # Preserve direct field access for older output readers while keeping structured candidates.
    for field_name, value in (result.get("resolved_values", {}) or {}).items():
        result.setdefault(field_name, value)
    if attachments.get("best_attachment_total") is not None:
        result.setdefault("attachment_total", attachments.get("best_attachment_total"))
    if schedule_e.get("total") is not None:
        result.setdefault("schedule_e_total", schedule_e.get("total"))
    if schedule_values.get("good_faith_total") is not None:
        result.setdefault("good_faith_value", schedule_values.get("good_faith_total"))
    if valuation_result.get("final_recommended_value") is not None:
        result.setdefault("rendered_value", valuation_result.get("final_recommended_value"))

    result["assessment_summary"] = AssessmentSummaryBuilder().build_summary(
        rendition_result=result,
        manual_override=manual_override,
        depreciated_override_result=depreciated_override_result,
    )

    result["agent_review"] = review_parse_result(result)
    return result


if __name__ == "__main__":
    args = build_arg_parser().parse_args()

    if args.pages_json:
        pages = load_pages_from_json(args.pages_json)
    elif args.pages_txt_folder:
        pages = load_pages_from_txt_folder(args.pages_txt_folder)
    else:
        pages = get_demo_pages()

    print_loaded_page_preview(pages)

    manual_override = load_manual_override(args.manual_override_json)

    pipeline = Pipeline()
    result = pipeline.run(pages=pages, manual_override=manual_override)
    result.final_result["agent_review"] = review_parse_result(result.final_result)

    print("\nFINAL RESULT")
    print(result.final_result)

    print_pipeline_debug(result)
