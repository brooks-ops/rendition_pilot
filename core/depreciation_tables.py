from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

DISPLAY_YEAR_COUNT = 15

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


def default_schedule_path(project_root: Path | None = None) -> Path:
    base_dir = project_root or Path(__file__).resolve().parent.parent
    return base_dir / "Data" / "depreciation_schedule.csv"


def normalize_factor_sequence(
    factors: list[float],
    *,
    minimum_length: int = DISPLAY_YEAR_COUNT,
) -> list[float]:
    normalized = [round(float(value), 2) for value in factors]
    if not normalized:
        raise ValueError("Depreciation factor sequence cannot be empty.")
    floor = normalized[-1]
    while len(normalized) < minimum_length:
        normalized.append(floor)
    return normalized


def csv_factor_sequence(schedule_df: pd.DataFrame, life_years: int) -> list[float]:
    matches = (
        schedule_df[schedule_df["life_years"] == life_years]
        .sort_values("acquisition_year", ascending=False)
    )
    if matches.empty:
        raise ValueError(f"No depreciation schedule found for {life_years}-year assets.")
    factors = matches["percent_good"].astype(float).tolist()
    return normalize_factor_sequence(factors)


def load_depreciation_tables(schedule_path: Path | None = None) -> dict[str, TableDefinition]:
    path = schedule_path or default_schedule_path()
    schedule_df = pd.read_csv(path)

    tables: dict[str, TableDefinition] = {}
    for table_key, metadata in TABLE_METADATA.items():
        factors = PROMPT_FACTOR_SEQUENCES.get(table_key)
        if factors is None:
            factors = csv_factor_sequence(schedule_df, metadata["life_years"])
        else:
            factors = normalize_factor_sequence(factors)

        tables[table_key] = TableDefinition(
            key=table_key,
            label=str(metadata["label"]),
            life_years=int(metadata["life_years"]),
            factors=factors,
            prior_factor=float(factors[-1]),
        )

    return tables


def available_depreciation_table_keys() -> list[str]:
    return list(TABLE_METADATA.keys())
