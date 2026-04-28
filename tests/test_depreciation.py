from pathlib import Path

from app.depreciation import DepreciationEngine


def test_depreciation_uses_newest_available_year_for_future_acquisition_year() -> None:
    schedule_path = Path(__file__).resolve().parent.parent / "Data" / "depreciation_schedule.csv"
    engine = DepreciationEngine(str(schedule_path))

    percent_good = engine.get_percent_by_life(9, 2026)

    assert percent_good == 0.90
