from app.rendition_calculator import (
    build_calculator_rows,
    build_flat_value_rows,
    build_freeport_rows,
    build_saved_calculator,
    calculate_freeport_exemption,
    calculate_combined_total,
    calculate_section_total,
    load_depreciation_tables,
)
from core.depreciation_tables import build_depreciation_table_sanity_snapshot
from core.valuation_engine import (
    build_schedule_a_rows,
    build_schedule_b_rows,
    build_schedule_c_rows,
    build_schedule_d_rows,
    build_schedule_e_rows,
)


def test_build_calculator_rows_uses_selected_tax_year_and_prior_bucket():
    rows = build_calculator_rows("8_year", 2026, costs={"2026": 1000, "prior": 500})

    assert rows[0]["display_year"] == "2025"
    assert rows[0]["factor"] == 0.75
    assert rows[0]["value"] == 0.0
    assert rows[-1]["display_year"] == "2010 & Prior"
    assert rows[-1]["factor"] == 0.05
    assert rows[-1]["value"] == 25.0


def test_five_year_table_uses_distinct_five_year_factors():
    rows = build_calculator_rows("5_year", 2026, costs={"2024": 1000, "2018": 1000, "prior": 1000})

    factor_by_year = {row["display_year"]: row["factor"] for row in rows}

    assert factor_by_year["2024"] == 0.45
    assert factor_by_year["2021"] == 0.10
    assert factor_by_year["2018"] == 0.10
    assert factor_by_year["2010 & Prior"] == 0.10


def test_five_year_table_does_not_match_eight_year_factors():
    five_year_rows = build_calculator_rows("5_year", 2026, costs={"2024": 1000})
    eight_year_rows = build_calculator_rows("8_year", 2026, costs={"2024": 1000})

    five_year_factor_by_year = {row["display_year"]: row["factor"] for row in five_year_rows}
    eight_year_factor_by_year = {row["display_year"]: row["factor"] for row in eight_year_rows}

    assert five_year_factor_by_year["2024"] == 0.45
    assert eight_year_factor_by_year["2024"] == 0.60


def test_nine_and_twelve_year_tables_load_from_existing_schedule():
    tables = load_depreciation_tables()

    assert tables["9_year"].factors[:4] == [0.90, 0.80, 0.70, 0.60]
    assert tables["12_year"].factors[:4] == [0.90, 0.80, 0.70, 0.65]
    assert len(tables["9_year"].factors) == 15
    assert len(tables["12_year"].factors) == 15


def test_saved_calculator_payload_and_combined_total():
    rows = build_calculator_rows("8_year", 2026, costs={"2025": 1000, "2024": 500})
    calculator = build_saved_calculator(
        name="Schedule A - Machinery & Equipment",
        schedule="A",
        category="Machinery & Equipment",
        depreciation_table="8_year",
        tax_year=2025,
        rows=rows,
    )

    assert calculator["section_total"] == calculate_section_total(rows)
    assert calculator["rows"][0]["display_year"] == "2025"
    assert calculator["rows"][-1]["display_year"] == "2010 & Prior"
    assert calculate_combined_total([calculator, {"section_total": 1250.25}]) == round(calculator["section_total"] + 1250.25, 2)


def test_flat_value_rows_build_single_total_row():
    rows = build_flat_value_rows("Schedule B - Inventory", 12500)

    assert rows == [
        {
            "bucket": "flat_value",
            "display_year": "Schedule B - Inventory",
            "year_acquired": None,
            "cost": 12500.0,
            "factor": 1.0,
            "value": 12500.0,
        }
    ]
    assert calculate_section_total(rows) == 12500.0


def test_freeport_exemption_calculates_percentage_exempt_and_taxable_values():
    result = calculate_freeport_exemption(10_000_000, 6_000_000, 12_000_000)

    assert result["freeport_percentage"] == 0.6
    assert result["freeport_exempt_amount"] == 7_200_000
    assert result["taxable_inventory_value"] == 4_800_000

    rows = build_freeport_rows(10_000_000, 6_000_000, 12_000_000)
    assert rows[-1]["display_year"] == "Remaining Taxable Inventory Value"
    assert calculate_section_total(rows) == 4_800_000


def test_freeport_exemption_requires_positive_prior_year_total():
    try:
        calculate_freeport_exemption(0, 6_000_000, 12_000_000)
    except ValueError as exc:
        assert "prior year total inventory" in str(exc)
    else:
        raise AssertionError("Expected invalid prior total to raise ValueError.")


def test_freeport_exemption_rejects_negative_values():
    try:
        calculate_freeport_exemption(10_000_000, -1, 12_000_000)
    except ValueError as exc:
        assert "cannot be negative" in str(exc)
    else:
        raise AssertionError("Expected negative Freeport value to raise ValueError.")


def test_schedule_b_rows_match_existing_flat_value_contract():
    assert build_schedule_b_rows(12500) == build_flat_value_rows("Schedule B - Inventory", 12500)


def test_schedule_c_rows_match_existing_flat_value_contract():
    assert build_schedule_c_rows(850) == build_flat_value_rows("Schedule C - Supplies", 850)


def test_schedule_d_auto_roll_value_can_be_added_to_depreciation_rows():
    rows = build_flat_value_rows("Auto Roll Value", 4000) + build_calculator_rows("9_year", 2026, costs={"2025": 10000})

    assert rows[0]["bucket"] == "flat_value"
    assert rows[0]["value"] == 4000.0
    assert calculate_section_total(rows) == 13000.0


def test_schedule_d_rows_match_existing_nine_year_contract():
    tables = load_depreciation_tables()
    costs = {"2025": 10000, "2024": 5000, "prior": 2500}

    assert build_schedule_d_rows(2026, costs=costs, tables=tables) == build_calculator_rows(
        "9_year",
        2026,
        costs=costs,
        tables=tables,
    )


def test_schedule_e_rows_match_existing_five_year_contract():
    tables = load_depreciation_tables()
    costs = {"2025": 6000, "2024": 3000, "prior": 1500}

    assert build_schedule_e_rows(2026, costs=costs, tables=tables) == build_calculator_rows(
        "5_year",
        2026,
        costs=costs,
        tables=tables,
    )


def test_schedule_a_rows_respect_selected_manual_depreciation_table():
    tables = load_depreciation_tables()
    costs = {"2025": 1000, "2024": 1000, "prior": 1000}

    assert build_schedule_a_rows(2026, depreciation_table="8_year", costs=costs, tables=tables) == build_calculator_rows(
        "8_year",
        2026,
        costs=costs,
        tables=tables,
    )


def test_schedule_d_rows_respect_selected_manual_depreciation_table():
    tables = load_depreciation_tables()
    costs = {"2025": 1000, "2024": 1000, "prior": 1000}

    assert build_schedule_d_rows(2026, depreciation_table="12_year", costs=costs, tables=tables) == build_calculator_rows(
        "12_year",
        2026,
        costs=costs,
        tables=tables,
    )


def test_schedule_e_rows_respect_selected_manual_depreciation_table():
    tables = load_depreciation_tables()
    costs = {"2025": 1000, "2024": 1000, "prior": 1000}

    assert build_schedule_e_rows(2026, depreciation_table="9_year", costs=costs, tables=tables) == build_calculator_rows(
        "9_year",
        2026,
        costs=costs,
        tables=tables,
    )


def test_depreciation_table_sanity_snapshot_keeps_manual_tables_distinct():
    snapshot = build_depreciation_table_sanity_snapshot(tax_year=2026, comparison_year=2022, sample_cost=1000.0)

    assert snapshot["5_year"] == 150.0
    assert snapshot["8_year"] == 350.0
    assert snapshot["9_year"] == 600.0
    assert snapshot["12_year"] == 650.0
