from dataclasses import dataclass

@dataclass
class AssetRow:
    description: str
    acquisition_year: int | None
    original_cost: float | None
    matched_category: str | None = None
    depreciation_percent: float | None = None
    assessed_value: float | None = None
    confidence: str = "Needs review"
    notes: str = ""