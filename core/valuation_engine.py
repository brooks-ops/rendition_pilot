from __future__ import annotations

from app.depreciation import DepreciationEngine
from core.depreciation_tables import default_schedule_path


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
