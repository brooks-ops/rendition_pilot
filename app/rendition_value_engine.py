from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from app.depreciation import DepreciationEngine
from app.targeted_parser import TargetedRenditionParser, parse_money_text

SCHEDULE_E_LIFE_YEARS = {
    "furniture_fixtures": 9,
    "machinery_equipment": 9,
    "office_equipment": 8,
    "computer_equipment": 5,
    "pos_servers_mainframes": 5,
    "other": 9,
}

SCHEDULE_KEY_ORDER = ["A", "B", "C", "D", "E"]
SCHEDULE_E_BREAKDOWN_KEYS = [
    "furniture_fixtures",
    "machinery_equipment",
    "office_equipment",
    "computer_equipment",
    "pos_servers_mainframes",
    "other",
]
MAX_TRUSTED_EXTRACTED_VALUE = 20_000_000.0

MONEY_TOKEN_RE = re.compile(
    r"\$?\s*[0-9Oo][0-9Oo,\s.]{0,20}(?:\.\d{1,2})?"
)
YEAR_RE = re.compile(r"\b(19[5-9]\d|20[0-4]\d)\b")


@dataclass
class RenditionLineItem:
    schedule: str
    subsection: str | None = None
    year_acquired: int | None = None
    historical_cost: float | None = None
    good_faith_value: float | None = None
    raw_text: str = ""
    source_page: int | None = None
    confidence: float | None = None
    flags: list[str] = field(default_factory=list)
    exact_value: float | None = None
    raw_values: dict[str, Any] = field(default_factory=dict)
    calculated_value: float | None = None
    depreciation_factor: float | None = None
    value_source: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ValuationResult:
    schedule_totals: dict[str, float]
    subsection_totals: dict[str, float]
    final_recommended_value: float | None
    line_items: list[dict[str, Any]]
    flags: list[str]
    debug_summary: dict[str, Any]
    confidence: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_default_depreciation_engine() -> DepreciationEngine | None:
    schedule_path = Path(__file__).resolve().parent.parent / "Data" / "depreciation_schedule.csv"
    if not schedule_path.exists():
        return None
    return DepreciationEngine(str(schedule_path))


def get_depreciated_value(
    cost: float,
    year_acquired: int,
    life_years: int,
    *,
    engine: DepreciationEngine | None = None,
) -> tuple[float | None, float | None]:
    depreciation_engine = engine or load_default_depreciation_engine()
    if depreciation_engine is None:
        return None, None

    factor, value = depreciation_engine.assess_value(
        original_cost=float(cost),
        acquisition_year=int(year_acquired),
        life_years=int(life_years),
    )
    return value, factor


def calculate_schedule_a(
    line_items: Iterable[RenditionLineItem | dict[str, Any]],
    *,
    engine: DepreciationEngine | None = None,
) -> dict[str, Any]:
    return _calculate_schedule(
        "A",
        line_items,
        life_years=9,
        engine=engine,
    )


def calculate_schedule_b(
    line_items: Iterable[RenditionLineItem | dict[str, Any]],
) -> dict[str, Any]:
    return _calculate_schedule(
        "B",
        line_items,
        use_exact_value=True,
    )


def calculate_schedule_c(
    line_items: Iterable[RenditionLineItem | dict[str, Any]],
) -> dict[str, Any]:
    return _calculate_schedule(
        "C",
        line_items,
        use_exact_value=True,
    )


def calculate_schedule_d(
    line_items: Iterable[RenditionLineItem | dict[str, Any]],
    *,
    engine: DepreciationEngine | None = None,
) -> dict[str, Any]:
    return _calculate_schedule(
        "D",
        line_items,
        life_years=9,
        engine=engine,
    )


def calculate_schedule_e(
    line_items: Iterable[RenditionLineItem | dict[str, Any]],
    *,
    engine: DepreciationEngine | None = None,
) -> dict[str, Any]:
    normalized = [
        item for item in _normalize_line_items(line_items)
        if item.schedule.upper() == "E"
    ]
    flags: list[str] = []
    subsection_totals = {key: 0.0 for key in SCHEDULE_E_BREAKDOWN_KEYS}
    evaluated_items: list[dict[str, Any]] = []
    debug_rows: list[dict[str, Any]] = []

    for item in normalized:
        subsection_key = _normalize_subsection_name(item.subsection)
        if subsection_key not in SCHEDULE_E_LIFE_YEARS:
            item.flags.append("unknown_schedule_e_subsection")
            flags.append("unknown_schedule_e_subsection")
            subsection_key = "other"
            item.subsection = "other"

        evaluated, row_flags, debug_row = _evaluate_line_item(
            item,
            life_years=SCHEDULE_E_LIFE_YEARS[subsection_key],
            engine=engine,
        )
        subsection_totals[subsection_key] = round(subsection_totals[subsection_key] + (evaluated or 0.0), 2)
        evaluated_items.append(item.to_dict())
        debug_rows.append(debug_row)
        flags.extend(row_flags)

    year_based_items = [
        item
        for item in normalized
        if item.year_acquired is not None and item.historical_cost is not None
    ]
    populated_subsections = {
        _normalize_subsection_name(item.subsection)
        for item in year_based_items
        if _normalize_subsection_name(item.subsection)
    }
    header_based_matches = sum(
        1 for item in year_based_items if bool((item.raw_values or {}).get("header_subsection_match"))
    )
    if len(year_based_items) >= 3 and len(populated_subsections) <= 1 and header_based_matches < len(year_based_items):
        flags.append("ambiguous_schedule_e_subsection_mapping")

    total = round(sum(subsection_totals.values()), 2)
    return {
        "total": total,
        "flags": sorted(set(flags)),
        "subsection_totals": subsection_totals,
        "evaluated_items": evaluated_items,
        "debug_rows": debug_rows,
    }


def calculate_rendition_value(
    parsed_result: dict[str, Any],
    *,
    engine: DepreciationEngine | None = None,
) -> dict[str, Any]:
    depreciation_engine = engine or load_default_depreciation_engine()
    tax_year = _resolve_tax_year(parsed_result)
    line_items = extract_line_items(parsed_result)

    grouped: dict[str, list[RenditionLineItem]] = {letter: [] for letter in SCHEDULE_KEY_ORDER}
    ignored_items: list[dict[str, Any]] = []
    for item in line_items:
        schedule = item.schedule.upper()
        if schedule == "F":
            ignored_items.append(item.to_dict())
            continue
        if schedule in grouped:
            grouped[schedule].append(item)

    schedule_a = calculate_schedule_a(grouped["A"], engine=depreciation_engine)
    schedule_b = calculate_schedule_b(grouped["B"])
    schedule_c = calculate_schedule_c(grouped["C"])
    schedule_d = calculate_schedule_d(grouped["D"], engine=depreciation_engine)
    schedule_e = calculate_schedule_e(grouped["E"], engine=depreciation_engine)

    schedule_totals = {
        "A": round(float(schedule_a["total"]), 2),
        "B": round(float(schedule_b["total"]), 2),
        "C": round(float(schedule_c["total"]), 2),
        "D": round(float(schedule_d["total"]), 2),
        "E": round(float(schedule_e["total"]), 2),
    }
    final_value = round(sum(schedule_totals.values()), 2)

    all_items = (
        schedule_a["evaluated_items"]
        + schedule_b["evaluated_items"]
        + schedule_c["evaluated_items"]
        + schedule_d["evaluated_items"]
        + schedule_e["evaluated_items"]
    )
    flags = sorted(
        set(
            schedule_a["flags"]
            + schedule_b["flags"]
            + schedule_c["flags"]
            + schedule_d["flags"]
            + schedule_e["flags"]
            + (["schedule_f_ignored"] if ignored_items else [])
        )
    )

    confidence = _derive_confidence(all_items, flags)
    has_any_calculated_value = any(item.get("calculated_value") is not None for item in all_items)
    if "ambiguous_schedule_e_subsection_mapping" in flags:
        has_any_calculated_value = False
    result = ValuationResult(
        schedule_totals=schedule_totals,
        subsection_totals=schedule_e["subsection_totals"],
        final_recommended_value=final_value if has_any_calculated_value else None,
        line_items=all_items,
        flags=flags,
        debug_summary={
            "tax_year": tax_year,
            "line_item_count": len(all_items),
            "ignored_schedule_f_items": ignored_items,
            "schedule_debug": {
                "A": schedule_a["debug_rows"],
                "B": schedule_b["debug_rows"],
                "C": schedule_c["debug_rows"],
                "D": schedule_d["debug_rows"],
                "E": schedule_e["debug_rows"],
            },
            "depreciation_table_loaded": depreciation_engine is not None,
        },
        confidence=confidence,
    )
    return result.to_dict()


def extract_line_items(parsed_result: dict[str, Any]) -> list[RenditionLineItem]:
    existing = parsed_result.get("extracted_line_items") or parsed_result.get("line_items")
    if existing:
        return _normalize_line_items(existing)

    pages = _extract_pages(parsed_result)
    if not pages:
        return []

    items: list[RenditionLineItem] = []
    targeted_parser = TargetedRenditionParser()

    for page in pages:
        page_number = int(page.get("page_number", 1) or 1)
        page_text = str(page.get("text", "") or "")
        sections = _split_schedule_sections(page_text)
        schedule_e_detected = targeted_parser.parse_schedule_e_total(page_text).get("schedule_e_present")

        for schedule, section_text in sections.items():
            if schedule == "F":
                continue
            if schedule in {"A", "D"}:
                items.extend(_parse_schedule_ad_text_rows(schedule, section_text, page_number))
            elif schedule in {"B", "C"}:
                items.extend(_parse_schedule_bc_text_rows(schedule, section_text, page_number))

        words = page.get("ocr_blocks", []) or []
        if schedule_e_detected:
            schedule_e_rows = targeted_parser.parse_schedule_e_subsection_rows(words)
            for row in schedule_e_rows:
                items.append(
                    RenditionLineItem(
                        schedule="E",
                        subsection=row.get("subsection"),
                        year_acquired=row.get("year_acquired"),
                        historical_cost=row.get("historical_cost"),
                        good_faith_value=row.get("good_faith_value"),
                        raw_text=str(row.get("raw_text", "")),
                        source_page=page_number,
                        confidence=row.get("confidence"),
                        flags=list(row.get("flags") or []),
                        raw_values=dict(row.get("raw_values") or {}),
                    )
                )

        # Schedule E is the most layout-sensitive page in the form. When we have
        # OCR word geometry, do not fall back to line-based text parsing because
        # collapsed OCR lines can merge adjacent cells and create invented amounts.
        if "E" in sections and schedule_e_detected and not schedule_e_rows and not words:
            items.extend(_parse_schedule_e_text_rows(sections["E"], page_number))

    return _dedupe_line_items(items)


def _calculate_schedule(
    schedule: str,
    line_items: Iterable[RenditionLineItem | dict[str, Any]],
    *,
    life_years: int | None = None,
    use_exact_value: bool = False,
    engine: DepreciationEngine | None = None,
) -> dict[str, Any]:
    normalized = [
        item for item in _normalize_line_items(line_items)
        if item.schedule.upper() == schedule.upper()
    ]
    flags: list[str] = []
    total = 0.0
    evaluated_items: list[dict[str, Any]] = []
    debug_rows: list[dict[str, Any]] = []

    for item in normalized:
        evaluated, row_flags, debug_row = _evaluate_line_item(
            item,
            life_years=life_years,
            use_exact_value=use_exact_value,
            engine=engine,
        )
        total = round(total + (evaluated or 0.0), 2)
        evaluated_items.append(item.to_dict())
        debug_rows.append(debug_row)
        flags.extend(row_flags)

    return {
        "total": total,
        "flags": sorted(set(flags)),
        "evaluated_items": evaluated_items,
        "debug_rows": debug_rows,
    }


def _evaluate_line_item(
    item: RenditionLineItem,
    *,
    life_years: int | None = None,
    use_exact_value: bool = False,
    engine: DepreciationEngine | None = None,
) -> tuple[float | None, list[str], dict[str, Any]]:
    row_flags = list(item.flags)
    selected_value: float | None = None
    factor_used: float | None = None
    value_source: str | None = None

    if use_exact_value:
        selected_value = item.exact_value
        if selected_value is None:
            selected_value = item.good_faith_value
        if selected_value is None:
            selected_value = item.historical_cost
        value_source = "exact_value"
    elif item.good_faith_value is not None:
        selected_value = item.good_faith_value
        value_source = "good_faith_value"
    elif item.historical_cost is not None:
        if item.year_acquired is None:
            row_flags.append("missing_year")
            value_source = "historical_cost_missing_year"
        else:
            selected_value, factor_used = get_depreciated_value(
                item.historical_cost,
                item.year_acquired,
                int(life_years or 0),
                engine=engine,
            )
            value_source = "historical_cost_depreciated"
            if selected_value is None or factor_used is None:
                row_flags.append("missing_depreciation_factor")
    else:
        row_flags.append("unreadable_value")
        value_source = "no_usable_value"

    if selected_value is not None and float(selected_value) > MAX_TRUSTED_EXTRACTED_VALUE:
        selected_value = 0.0
        factor_used = None
        row_flags.append("value_over_trust_threshold")
        value_source = "value_zeroed_over_trust_threshold"

    if (item.confidence or 0) < 0.55:
        row_flags.append("low_confidence_ocr")

    item.flags = sorted(set(row_flags))
    item.calculated_value = selected_value
    item.depreciation_factor = factor_used
    item.value_source = value_source

    debug_row = {
        "schedule": item.schedule,
        "subsection": item.subsection,
        "source_page": item.source_page,
        "raw_text": item.raw_text,
        "raw_values": item.raw_values,
        "selected_value": selected_value,
        "value_source": value_source,
        "depreciation_factor": factor_used,
        "flags": item.flags,
    }
    return selected_value, item.flags, debug_row


def _normalize_line_items(
    line_items: Iterable[RenditionLineItem | dict[str, Any]],
) -> list[RenditionLineItem]:
    normalized: list[RenditionLineItem] = []
    for item in line_items or []:
        if isinstance(item, RenditionLineItem):
            normalized.append(item)
            continue

        row = dict(item)
        normalized.append(
            RenditionLineItem(
                schedule=str(row.get("schedule", "")).upper(),
                subsection=row.get("subsection"),
                year_acquired=_coerce_int(row.get("year_acquired")),
                historical_cost=_coerce_float(row.get("historical_cost")),
                good_faith_value=_coerce_float(row.get("good_faith_value")),
                raw_text=str(row.get("raw_text", "") or ""),
                source_page=_coerce_int(row.get("source_page")),
                confidence=_coerce_float(row.get("confidence")),
                flags=list(row.get("flags") or []),
                exact_value=_coerce_float(row.get("exact_value")),
                raw_values=dict(row.get("raw_values") or {}),
                calculated_value=_coerce_float(row.get("calculated_value")),
                depreciation_factor=_coerce_float(row.get("depreciation_factor")),
                value_source=row.get("value_source"),
            )
        )
    return _dedupe_line_items(normalized)


def _extract_pages(parsed_result: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(parsed_result.get("pages"), list):
        return [dict(page) for page in parsed_result.get("pages", []) if isinstance(page, dict)]

    page_texts = parsed_result.get("page_texts", [])
    if isinstance(page_texts, list) and page_texts:
        by_page = {
            int(entry.get("page_number", idx + 1) or idx + 1): {
                "page_number": int(entry.get("page_number", idx + 1) or idx + 1),
                "text": str(entry.get("text", "") or ""),
                "ocr_blocks": [],
            }
            for idx, entry in enumerate(page_texts)
            if isinstance(entry, dict)
        }
        return [by_page[key] for key in sorted(by_page)]

    return []


def _split_schedule_sections(page_text: str) -> dict[str, str]:
    matches = list(re.finditer(r"\bSCHEDULE\s+([A-F])\b", page_text or "", re.IGNORECASE))
    sections: dict[str, str] = {}
    for idx, match in enumerate(matches):
        letter = match.group(1).upper()
        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(page_text or "")
        sections[letter] = str(page_text or "")[start:end]
    return sections


def _parse_schedule_ad_text_rows(schedule: str, section_text: str, page_number: int) -> list[RenditionLineItem]:
    items: list[RenditionLineItem] = []
    for line in _candidate_lines(section_text):
        money_tokens = _parse_money_tokens(line)
        years = _parse_years(line)
        if not money_tokens:
            continue

        good_faith = None
        historical_cost = None
        year_acquired = years[-1] if years else None

        if year_acquired is not None:
            historical_cost = money_tokens[-1]["value"]
            if len(money_tokens) >= 2:
                good_faith = money_tokens[-2]["value"]
        elif len(money_tokens) >= 2:
            good_faith = money_tokens[-2]["value"]
            historical_cost = money_tokens[-1]["value"]
        else:
            good_faith = money_tokens[0]["value"]

        items.append(
            RenditionLineItem(
                schedule=schedule,
                subsection=None,
                year_acquired=year_acquired,
                historical_cost=historical_cost,
                good_faith_value=good_faith,
                raw_text=line,
                source_page=page_number,
                confidence=0.66,
                flags=[],
                raw_values={
                    "money_tokens": [token["raw"] for token in money_tokens],
                    "year_tokens": years,
                },
            )
        )
    return items


def _parse_schedule_bc_text_rows(schedule: str, section_text: str, page_number: int) -> list[RenditionLineItem]:
    items: list[RenditionLineItem] = []
    for line in _candidate_lines(section_text):
        money_tokens = _parse_money_tokens(line)
        if not money_tokens:
            continue

        exact_value = money_tokens[-1]["value"]
        items.append(
            RenditionLineItem(
                schedule=schedule,
                subsection=None,
                raw_text=line,
                source_page=page_number,
                confidence=0.7,
                flags=[],
                exact_value=exact_value,
                raw_values={"money_tokens": [token["raw"] for token in money_tokens]},
            )
        )
    return items


def _parse_schedule_e_text_rows(section_text: str, page_number: int) -> list[RenditionLineItem]:
    subsection = None
    subsection_items: list[RenditionLineItem] = []
    for line in _candidate_lines(section_text):
        normalized_line = line.lower()
        for key, phrases in {
            "furniture_fixtures": ["furniture and fixtures", "furniture fixtures"],
            "machinery_equipment": ["machinery and equipment", "machinery equipment"],
            "office_equipment": ["office equipment"],
            "computer_equipment": ["computer equipment"],
            "pos_servers_mainframes": ["pos", "servers", "mainframes"],
            "other": ["other"],
        }.items():
            if any(phrase in normalized_line for phrase in phrases):
                subsection = key
                break

        money_tokens = _parse_money_tokens(line)
        years = _parse_years(line)
        if subsection is None or not money_tokens:
            continue

        year_acquired = years[-1] if years else None
        historical_cost = None
        good_faith = None
        if year_acquired is not None:
            historical_cost = money_tokens[0]["value"]
            if len(money_tokens) >= 2:
                good_faith = money_tokens[-1]["value"]
        else:
            good_faith = money_tokens[-1]["value"]

        subsection_items.append(
            RenditionLineItem(
                schedule="E",
                subsection=subsection,
                year_acquired=year_acquired,
                historical_cost=historical_cost,
                good_faith_value=good_faith,
                raw_text=line,
                source_page=page_number,
                confidence=0.58,
                flags=["text_fallback_extraction"],
                raw_values={
                    "money_tokens": [token["raw"] for token in money_tokens],
                    "year_tokens": years,
                },
            )
        )
    return subsection_items


def _candidate_lines(section_text: str) -> list[str]:
    lines = []
    for raw_line in (section_text or "").splitlines():
        line = re.sub(r"\s+", " ", raw_line or "").strip()
        if not line:
            continue
        upper = line.upper()
        if upper.startswith("SCHEDULE "):
            continue
        if "TOTAL" in upper:
            continue
        if any(header in upper for header in ["GOOD FAITH", "HISTORICAL COST", "YEAR ACQUIRED", "ACTUAL COST"]):
            continue
        if len(line) < 4:
            continue
        lines.append(line)
    return lines


def _parse_money_tokens(text: str) -> list[dict[str, Any]]:
    tokens: list[dict[str, Any]] = []
    for match in MONEY_TOKEN_RE.finditer(text or ""):
        raw = match.group(0).strip()
        value = parse_money_text(raw)
        if value is None:
            continue
        if value < 100:
            continue
        if float(value).is_integer() and 1900 <= int(value) <= 2100:
            continue
        compact = raw.replace("$", "").strip()
        digit_groups = re.findall(r"\d{3,4}", compact)
        year_like_groups = [group for group in digit_groups if YEAR_RE.fullmatch(group)]
        if " " in compact and "," not in compact and "." not in compact and len(digit_groups) >= 2 and year_like_groups:
            continue
        tokens.append({"raw": raw, "value": value, "start": match.start()})
    return tokens


def _parse_years(text: str) -> list[int]:
    years: list[int] = []
    for match in YEAR_RE.finditer(text or ""):
        years.append(int(match.group(1)))
    return years


def _dedupe_line_items(items: Iterable[RenditionLineItem]) -> list[RenditionLineItem]:
    deduped: dict[tuple[Any, ...], RenditionLineItem] = {}
    for item in items:
        subsection = _normalize_subsection_name(item.subsection)
        if subsection:
            item.subsection = subsection
        key = (
            item.schedule.upper(),
            subsection,
            item.source_page,
            item.year_acquired,
            item.historical_cost,
            item.good_faith_value,
            item.exact_value,
            re.sub(r"\s+", " ", item.raw_text or "").strip().upper(),
        )
        existing = deduped.get(key)
        if existing is None or float(item.confidence or 0) > float(existing.confidence or 0):
            deduped[key] = item
    return list(deduped.values())


def _resolve_tax_year(parsed_result: dict[str, Any]) -> int:
    metadata = parsed_result.get("metadata", {}) or {}
    tax_year = _coerce_int(metadata.get("tax_year"))
    if tax_year is not None:
        return tax_year
    return datetime.now().year


def _derive_confidence(line_items: list[dict[str, Any]], flags: list[str]) -> str:
    if not line_items:
        return "low"
    if any(flag in {"missing_year", "unreadable_value", "missing_depreciation_factor"} for flag in flags):
        return "medium" if any(item.get("calculated_value") is not None for item in line_items) else "low"
    if any(flag == "low_confidence_ocr" for flag in flags):
        return "medium"
    return "high"


def _normalize_subsection_name(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")
    return normalized or None


def _coerce_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
