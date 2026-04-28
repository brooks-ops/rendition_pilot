from datetime import datetime
import pandas as pd

CURRENT_YEAR = datetime.now().year


class DepreciationEngine:
    def __init__(self, schedule_path: str):
        self.df = pd.read_csv(schedule_path)

    def get_percent_by_life(self, life_years: int | None, acquisition_year: int | None) -> float | None:
        if life_years is None or acquisition_year is None:
            return None

        life_matches = self.df[self.df["life_years"] == life_years]
        if life_matches.empty:
            return None

        matches = life_matches[life_matches["acquisition_year"] == acquisition_year]

        if not matches.empty:
            return float(matches.iloc[0]["percent_good"])

        min_year = int(life_matches["acquisition_year"].min())
        max_year = int(life_matches["acquisition_year"].max())

        if acquisition_year < min_year:
            oldest_match = life_matches[life_matches["acquisition_year"] == min_year]
            return float(oldest_match.iloc[0]["percent_good"])

        if acquisition_year > max_year:
            newest_match = life_matches[life_matches["acquisition_year"] == max_year]
            return float(newest_match.iloc[0]["percent_good"])

        nearest = life_matches.assign(
            acquisition_year_distance=(life_matches["acquisition_year"] - acquisition_year).abs()
        ).sort_values(by=["acquisition_year_distance", "acquisition_year"], ascending=[True, False])
        if nearest.empty:
            return None

        return float(nearest.iloc[0]["percent_good"])

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
