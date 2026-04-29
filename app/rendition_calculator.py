from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd

DISPLAY_YEAR_COUNT = 15

SECTION_PRESETS = {
    "schedule_a_furniture": {
        "label": "Schedule A - Furniture & Fixtures",
        "schedule": "A",
        "category": "Furniture & Fixtures",
        "default_table": "9_year",
        "entry_mode": "depreciation",
    },
    "schedule_a_machinery": {
        "label": "Schedule A - Machinery & Equipment",
        "schedule": "A",
        "category": "Machinery & Equipment",
        "default_table": "9_year",
        "entry_mode": "depreciation",
    },
    "schedule_b_inventory": {
        "label": "Schedule B - Inventory",
        "schedule": "B",
        "category": "Inventory",
        "default_table": "flat",
        "entry_mode": "flat",
    },
    "schedule_c_supplies": {
        "label": "Schedule C - Supplies",
        "schedule": "C",
        "category": "Supplies",
        "default_table": "flat",
        "entry_mode": "flat",
    },
    "schedule_d_vehicles": {
        "label": "Schedule D - Vehicles",
        "schedule": "D",
        "category": "Vehicles",
        "default_table": "9_year",
        "entry_mode": "depreciation",
        "supplemental_flat_label": "Auto Roll Value",
    },
    "schedule_e_computers": {
        "label": "Schedule E - Computers",
        "schedule": "E",
        "category": "Computers",
        "default_table": "5_year",
        "entry_mode": "depreciation",
    },
    "custom": {
        "label": "Custom",
        "schedule": "Custom",
        "category": "Custom",
        "default_table": "8_year",
        "entry_mode": "depreciation",
    },
}

TABLE_METADATA = {
    "5_year": {"label": "5 year", "life_years": 5},
    "8_year": {"label": "8 year", "life_years": 8},
    "9_year": {"label": "9 year", "life_years": 9},
    "12_year": {"label": "12 year", "life_years": 12},
}

# Match the worksheet-style factors the appraisers already use when they differ
# from the CSV-backed schedule. The 5-year table must stay distinct from 8-year.
PROMPT_FACTOR_SEQUENCES = {
    "5_year": [0.75, 0.45, 0.20, 0.15, 0.10],
    "8_year": [0.75, 0.60, 0.45, 0.35, 0.25, 0.20, 0.10, 0.05],
}


@dataclass(frozen=True)
class TableDefinition:
    key: str
    label: str
    life_years: int
    factors: list[float]
    prior_factor: float


def _default_schedule_path() -> Path:
    return Path(__file__).resolve().parent.parent / "Data" / "depreciation_schedule.csv"


def _normalize_factor_sequence(factors: list[float], *, minimum_length: int = DISPLAY_YEAR_COUNT) -> list[float]:
    normalized = [round(float(value), 2) for value in factors]
    if not normalized:
        raise ValueError("Depreciation factor sequence cannot be empty.")
    floor = normalized[-1]
    while len(normalized) < minimum_length:
        normalized.append(floor)
    return normalized


def _csv_factor_sequence(schedule_df: pd.DataFrame, life_years: int) -> list[float]:
    matches = (
        schedule_df[schedule_df["life_years"] == life_years]
        .sort_values("acquisition_year", ascending=False)
    )
    if matches.empty:
        raise ValueError(f"No depreciation schedule found for {life_years}-year assets.")
    factors = matches["percent_good"].astype(float).tolist()
    return _normalize_factor_sequence(factors)


def load_depreciation_tables(schedule_path: Path | None = None) -> dict[str, TableDefinition]:
    path = schedule_path or _default_schedule_path()
    schedule_df = pd.read_csv(path)

    tables: dict[str, TableDefinition] = {}
    for table_key, metadata in TABLE_METADATA.items():
        factors = PROMPT_FACTOR_SEQUENCES.get(table_key)
        if factors is None:
            factors = _csv_factor_sequence(schedule_df, metadata["life_years"])
        else:
            factors = _normalize_factor_sequence(factors)

        tables[table_key] = TableDefinition(
            key=table_key,
            label=str(metadata["label"]),
            life_years=int(metadata["life_years"]),
            factors=factors,
            prior_factor=float(factors[-1]),
        )

    return tables


def resolve_tax_year(candidate: Any = None) -> int:
    if candidate is None or candidate == "":
        return datetime.now().year
    try:
        return int(candidate)
    except (TypeError, ValueError):
        return datetime.now().year


def build_calculator_rows(
    table_key: str,
    tax_year: int,
    *,
    costs: dict[str, Any] | None = None,
    tables: dict[str, TableDefinition] | None = None,
) -> list[dict[str, Any]]:
    definitions = tables or load_depreciation_tables()
    table = definitions[table_key]
    cost_map = costs or {}
    rows: list[dict[str, Any]] = []
    base_year = int(tax_year) - 1

    for offset, factor in enumerate(table.factors):
        year = base_year - offset
        bucket = str(year)
        cost = round(float(cost_map.get(bucket, 0.0) or 0.0), 2)
        value = round(cost * factor, 2)
        rows.append(
            {
                "bucket": bucket,
                "display_year": str(year),
                "year_acquired": year,
                "cost": cost,
                "factor": round(float(factor), 2),
                "value": value,
            }
        )

    prior_year = base_year - len(table.factors)
    prior_cost = round(float(cost_map.get("prior", 0.0) or 0.0), 2)
    rows.append(
        {
            "bucket": "prior",
            "display_year": f"{prior_year} & Prior",
            "year_acquired": prior_year,
            "cost": prior_cost,
            "factor": round(float(table.prior_factor), 2),
            "value": round(prior_cost * table.prior_factor, 2),
        }
    )
    return rows


def calculate_section_total(rows: list[dict[str, Any]]) -> float:
    return round(sum(float(row.get("value", 0.0) or 0.0) for row in rows), 2)


def build_flat_value_rows(label: str, value: Any) -> list[dict[str, Any]]:
    amount = round(float(value or 0.0), 2)
    return [
        {
            "bucket": "flat_value",
            "display_year": str(label),
            "year_acquired": None,
            "cost": amount,
            "factor": 1.0,
            "value": amount,
        }
    ]


def generate_calculator_name(schedule: str, category: str) -> str:
    schedule_text = str(schedule).strip().upper()
    category_text = str(category).strip()
    if schedule_text and schedule_text != "CUSTOM":
        return f"Schedule {schedule_text} - {category_text}"
    return category_text or "Custom Calculator"


def build_saved_calculator(
    *,
    name: str,
    schedule: str,
    category: str,
    depreciation_table: str,
    tax_year: int,
    rows: list[dict[str, Any]],
    calculator_id: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    timestamp = datetime.now(timezone.utc).isoformat()
    section_total = calculate_section_total(rows)

    return {
        "id": calculator_id or str(uuid4()),
        "name": name,
        "schedule": schedule,
        "category": category,
        "depreciation_table": depreciation_table,
        "tax_year": int(tax_year),
        "rows": [
            {
                "bucket": row["bucket"],
                "display_year": row["display_year"],
                "year_acquired": row["year_acquired"],
                "cost": round(float(row["cost"]), 2),
                "factor": round(float(row["factor"]), 2),
                "value": round(float(row["value"]), 2),
            }
            for row in rows
        ],
        "section_total": section_total,
        "created_at": created_at or timestamp,
        "updated_at": timestamp,
    }


def calculate_combined_total(saved_calculators: list[dict[str, Any]]) -> float:
    return round(sum(float(item.get("section_total", 0.0) or 0.0) for item in saved_calculators), 2)
