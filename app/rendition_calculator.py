from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from core.depreciation_tables import (
    TABLE_METADATA,
    TableDefinition,
    load_depreciation_tables,
)

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
    "freeport_exemption": {
        "label": "Freeport",
        "schedule": "Freeport",
        "category": "Freeport Exemption",
        "default_table": "freeport",
        "entry_mode": "freeport",
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
    return round(
        sum(
            float(row.get("value", 0.0) or 0.0)
            for row in rows
            if row.get("include_in_total", True)
        ),
        2,
    )


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


def calculate_freeport_exemption(
    prior_year_total_inventory: Any,
    prior_year_freeport_eligible_inventory: Any,
    current_year_inventory: Any,
) -> dict[str, float]:
    try:
        prior_total = round(float(prior_year_total_inventory), 2)
        eligible = round(float(prior_year_freeport_eligible_inventory or 0.0), 2)
        current = round(float(current_year_inventory or 0.0), 2)
    except (TypeError, ValueError) as exc:
        raise ValueError("Enter a valid prior year total inventory value greater than zero.") from exc

    if prior_total <= 0:
        raise ValueError("Enter a valid prior year total inventory value greater than zero.")
    if eligible < 0 or current < 0:
        raise ValueError("Freeport values cannot be negative.")

    percentage = eligible / prior_total
    exempt_amount = round(current * percentage, 2)
    taxable_value = round(current - exempt_amount, 2)
    return {
        "prior_year_total_inventory": prior_total,
        "prior_year_freeport_eligible_inventory": eligible,
        "current_year_inventory": current,
        "freeport_percentage": percentage,
        "freeport_exempt_amount": exempt_amount,
        "taxable_inventory_value": taxable_value,
    }


def build_freeport_rows(
    prior_year_total_inventory: Any,
    prior_year_freeport_eligible_inventory: Any,
    current_year_inventory: Any,
) -> list[dict[str, Any]]:
    result = calculate_freeport_exemption(
        prior_year_total_inventory,
        prior_year_freeport_eligible_inventory,
        current_year_inventory,
    )
    percentage = result["freeport_percentage"]
    return [
        {
            "bucket": "prior_year_total_inventory",
            "display_year": "Prior Year Total Inventory Value",
            "year_acquired": None,
            "cost": result["prior_year_total_inventory"],
            "factor": 1.0,
            "value": result["prior_year_total_inventory"],
            "include_in_total": False,
        },
        {
            "bucket": "prior_year_freeport_eligible_inventory",
            "display_year": "Prior Year Freeport-Eligible Inventory Shipped Out of Texas Within 175 Days",
            "year_acquired": None,
            "cost": result["prior_year_freeport_eligible_inventory"],
            "factor": percentage,
            "value": result["freeport_exempt_amount"],
            "include_in_total": False,
        },
        {
            "bucket": "current_year_inventory",
            "display_year": "Current Year Inventory Value",
            "year_acquired": None,
            "cost": result["current_year_inventory"],
            "factor": percentage,
            "value": result["freeport_exempt_amount"],
            "include_in_total": False,
        },
        {
            "bucket": "taxable_inventory_value",
            "display_year": "Remaining Taxable Inventory Value",
            "year_acquired": None,
            "cost": result["current_year_inventory"],
            "factor": 1 - percentage,
            "value": result["taxable_inventory_value"],
            "include_in_total": True,
        },
    ]


def generate_calculator_name(schedule: str, category: str) -> str:
    if str(schedule).strip().lower() == "freeport":
        return "Freeport Exemption"
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
    freeport: dict[str, Any] | None = None,
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
                "include_in_total": bool(row.get("include_in_total", True)),
            }
            for row in rows
        ],
        "freeport": freeport,
        "section_total": section_total,
        "created_at": created_at or timestamp,
        "updated_at": timestamp,
    }


def calculate_combined_total(saved_calculators: list[dict[str, Any]]) -> float:
    return round(sum(float(item.get("section_total", 0.0) or 0.0) for item in saved_calculators), 2)
