from datetime import datetime
import pandas as pd

CURRENT_YEAR = datetime.now().year


class DepreciationEngine:
    def __init__(self, schedule_path: str):
        self.df = pd.read_csv(schedule_path)

    def get_percent_by_life(self, life_years: int | None, acquisition_year: int | None) -> float | None:
        if life_years is None or acquisition_year is None:
            return None

        matches = self.df[
            (self.df["life_years"] == life_years) &
            (self.df["acquisition_year"] == acquisition_year)
        ]

        if not matches.empty:
            return float(matches.iloc[0]["percent_good"])

        fallback = self.df[self.df["life_years"] == life_years]
        if fallback.empty:
            return None

        min_year = int(fallback["acquisition_year"].min())
        if acquisition_year < min_year:
            oldest_match = fallback[fallback["acquisition_year"] == min_year]
            return float(oldest_match.iloc[0]["percent_good"])
        return None

    def assess_value(
        self,
        original_cost: float | None,
        life_years: int | None,
        acquisition_year: int | None
    ) -> tuple[float | None, float | None]:
        if original_cost is None:
            return None, None

        pct = self.get_percent_by_life(life_years, acquisition_year)
        if pct is None:
            return None, None

        value = round(original_cost * pct, 2)
        return pct, value
