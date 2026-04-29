"""Minimal FastAPI app scaffold for backend endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from core.valuation_engine import (
    build_schedule_a_rows,
    build_schedule_b_rows,
    build_schedule_c_rows,
    build_schedule_d_rows,
    build_schedule_e_rows,
)


app = FastAPI(title="Rendition Pilot API")

# Local-development CORS only. This allows the static frontend and local dev
# servers to call the API without changing any backend logic.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "null",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DEPRECIATION_SCHEDULE_MAP = {
    "5": "5_year",
    "8": "8_year",
    "9": "9_year",
    "12": "12_year",
}


class CalculateRequest(BaseModel):
    schedule_type: Literal["A", "B", "C", "D", "E"]
    rows: list[dict[str, Any]]
    depreciation_schedule: Literal["5", "8", "9", "12"]


class CalculateResponse(BaseModel):
    total_value: float
    breakdown: list[dict[str, Any]] | None = None


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "rendition-pilot-api"}


@app.get("/api/info")
def info() -> dict[str, str]:
    return {
        "status": "ok",
        "service": "rendition-pilot-api",
        "version": "v1",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _to_float(value: Any) -> float:
    try:
        return round(float(value or 0.0), 2)
    except (TypeError, ValueError):
        return 0.0


def _extract_costs(rows: list[dict[str, Any]]) -> dict[str, float]:
    costs: dict[str, float] = {}
    for row in rows:
        bucket = str(row.get("bucket") or "").strip()
        if not bucket or bucket in {"flat_value", "good_faith_value"}:
            continue
        costs[bucket] = _to_float(row.get("cost"))
    return costs


def _extract_good_faith_value(rows: list[dict[str, Any]]) -> float | None:
    for row in rows:
        if str(row.get("bucket") or "").strip() == "good_faith_value":
            return _to_float(row.get("cost", row.get("value")))
    return None


def _extract_flat_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flat_rows: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("bucket") or "").strip() != "flat_value":
            continue
        amount = _to_float(row.get("cost", row.get("value")))
        flat_rows.append(
            {
                "bucket": "flat_value",
                "display_year": str(row.get("display_year") or "Flat Value"),
                "year_acquired": None,
                "cost": amount,
                "factor": 1.0,
                "value": amount,
            }
        )
    return flat_rows


def _infer_tax_year(rows: list[dict[str, Any]]) -> int | None:
    years: list[int] = []
    for row in rows:
        bucket = str(row.get("bucket") or "").strip()
        year_acquired = row.get("year_acquired")
        if bucket in {"flat_value", "good_faith_value", "prior"} or year_acquired in (None, ""):
            continue
        try:
            years.append(int(year_acquired))
        except (TypeError, ValueError):
            continue
    if not years:
        return None
    return max(years) + 1


def _calculate_breakdown(request: CalculateRequest) -> list[dict[str, Any]]:
    if not request.rows:
        raise HTTPException(status_code=400, detail="rows must contain at least one item.")

    for index, row in enumerate(request.rows):
        if not isinstance(row, dict):
            raise HTTPException(status_code=400, detail=f"rows[{index}] must be an object.")

    depreciation_table = DEPRECIATION_SCHEDULE_MAP[request.depreciation_schedule]

    if request.schedule_type == "B":
        amount = round(sum(_to_float(row.get("cost", row.get("value"))) for row in request.rows), 2)
        return build_schedule_b_rows(amount)

    if request.schedule_type == "C":
        amount = round(sum(_to_float(row.get("cost", row.get("value"))) for row in request.rows), 2)
        return build_schedule_c_rows(amount)

    tax_year = _infer_tax_year(request.rows)
    if tax_year is None:
        raise HTTPException(
            status_code=400,
            detail="Could not infer tax year from rows. Include at least one depreciation row with year_acquired.",
        )

    costs = _extract_costs(request.rows)
    good_faith_value = _extract_good_faith_value(request.rows)

    if request.schedule_type == "A":
        return build_schedule_a_rows(
            tax_year,
            depreciation_table=depreciation_table,
            costs=costs,
            good_faith_value=good_faith_value,
        )

    if request.schedule_type == "D":
        return _extract_flat_rows(request.rows) + build_schedule_d_rows(
            tax_year,
            depreciation_table=depreciation_table,
            costs=costs,
            good_faith_value=good_faith_value,
        )

    return build_schedule_e_rows(
        tax_year,
        depreciation_table=depreciation_table,
        costs=costs,
    )


@app.post("/api/calculate", response_model=CalculateResponse)
def calculate(request: CalculateRequest) -> CalculateResponse:
    breakdown = _calculate_breakdown(request)
    total_value = round(sum(_to_float(row.get("value")) for row in breakdown), 2)
    return CalculateResponse(total_value=total_value, breakdown=breakdown)
