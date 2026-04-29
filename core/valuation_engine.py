from __future__ import annotations

from typing import Any

from app.depreciation import DepreciationEngine
from core.depreciation_tables import TableDefinition, default_schedule_path, load_depreciation_tables


def calculate_depreciated_value(
    historical_cost: float,
    acquisition_year: int,
    life_years: int,
) -> tuple[float | None, float | None]:
    """Return percent-good and depreciated value for a historical-cost input."""
    schedule_path = default_schedule_path()
    if not schedule_path.exists():
        return None, None

    engine = DepreciationEngine(str(schedule_path))
    return engine.assess_value(
        original_cost=float(historical_cost),
        acquisition_year=int(acquisition_year),
        life_years=int(life_years),
    )


def build_schedule_a_rows(
    tax_year: int,
    *,
    costs: dict[str, Any] | None = None,
    good_faith_value: Any = None,
    tables: dict[str, TableDefinition] | None = None,
) -> list[dict[str, Any]]:
    """Schedule A adds Good Faith Estimate values as-is and depreciates Historical Cost values on the 9-year schedule by Year Acquired, then totals those adjusted values."""
    definitions = tables or load_depreciation_tables()
    table = definitions["9_year"]
    cost_map = costs or {}
    rows: list[dict[str, Any]] = []

    if good_faith_value is not None:
        amount = round(float(good_faith_value or 0.0), 2)
        rows.append(
            {
                "bucket": "good_faith_value",
                "display_year": "Good Faith Estimate",
                "year_acquired": None,
                "cost": amount,
                "factor": 1.0,
                "value": amount,
            }
        )

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
