from app.rendition_calculator import (
    build_calculator_rows,
    build_flat_value_rows,
    build_saved_calculator,
    calculate_combined_total,
    calculate_section_total,
    load_depreciation_tables,
)


def test_build_calculator_rows_uses_selected_tax_year_and_prior_bucket():
    rows = build_calculator_rows("8_year", 2026, costs={"2026": 1000, "prior": 500})

    assert rows[0]["display_year"] == "2025"
    assert rows[0]["factor"] == 0.75
    assert rows[0]["value"] == 0.0
    assert rows[-1]["display_year"] == "2010 & Prior"
    assert rows[-1]["factor"] == 0.05
    assert rows[-1]["value"] == 25.0


def test_five_year_table_uses_excel_style_prompt_factors():
    rows = build_calculator_rows("5_year", 2026, costs={"2024": 1000, "2018": 1000, "prior": 1000})

    factor_by_year = {row["display_year"]: row["factor"] for row in rows}

    assert factor_by_year["2024"] == 0.60
    assert factor_by_year["2018"] == 0.05
    assert factor_by_year["2010 & Prior"] == 0.05


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


def test_schedule_d_auto_roll_value_can_be_added_to_depreciation_rows():
    rows = build_flat_value_rows("Auto Roll Value", 4000) + build_calculator_rows("9_year", 2026, costs={"2025": 10000})

    assert rows[0]["bucket"] == "flat_value"
    assert rows[0]["value"] == 4000.0
    assert calculate_section_total(rows) == 13000.0
