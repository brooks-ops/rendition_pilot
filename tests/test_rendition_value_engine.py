from app.rendition_value_engine import (
    RenditionLineItem,
    _parse_money_tokens,
    calculate_rendition_value,
    calculate_schedule_a,
    calculate_schedule_b,
    calculate_schedule_c,
    calculate_schedule_d,
    calculate_schedule_e,
    get_depreciated_value,
)


def test_good_faith_beats_historical_cost_on_same_row():
    item = RenditionLineItem(
        schedule="A",
        good_faith_value=15000.0,
        historical_cost=50000.0,
        year_acquired=2023,
        raw_text="fixture row",
    )

    result = calculate_schedule_a([item])

    assert result["total"] == 15000.0
    assert result["evaluated_items"][0]["value_source"] == "good_faith_value"


def test_schedule_a_historical_cost_uses_9_year_table():
    expected_value, expected_factor = get_depreciated_value(10000.0, 2024, 9)

    result = calculate_schedule_a(
        [RenditionLineItem(schedule="A", historical_cost=10000.0, year_acquired=2024, raw_text="A row")]
    )

    assert result["total"] == expected_value
    assert result["evaluated_items"][0]["depreciation_factor"] == expected_factor


def test_schedule_b_inventory_uses_exact_value_without_depreciation():
    result = calculate_schedule_b(
        [RenditionLineItem(schedule="B", exact_value=18250.0, raw_text="inventory row")]
    )

    assert result["total"] == 18250.0
    assert result["evaluated_items"][0]["depreciation_factor"] is None


def test_schedule_c_supplies_uses_exact_value_without_depreciation():
    result = calculate_schedule_c(
        [RenditionLineItem(schedule="C", exact_value=725.0, raw_text="supplies row")]
    )

    assert result["total"] == 725.0
    assert result["evaluated_items"][0]["depreciation_factor"] is None


def test_schedule_d_historical_cost_uses_9_year_table():
    expected_value, expected_factor = get_depreciated_value(25000.0, 2022, 9)

    result = calculate_schedule_d(
        [RenditionLineItem(schedule="D", historical_cost=25000.0, year_acquired=2022, raw_text="truck row")]
    )

    assert result["total"] == expected_value
    assert result["evaluated_items"][0]["depreciation_factor"] == expected_factor


def test_parse_money_tokens_rejects_merged_year_strings_but_keeps_real_amounts():
    assert _parse_money_tokens("2021 2423 2024") == []
    assert _parse_money_tokens("2425 2025") == []
    assert _parse_money_tokens("10,424") == [{"raw": "10,424", "value": 10424.0, "start": 0}]


def test_schedule_e_furniture_uses_9_year_table():
    expected_value, _expected_factor = get_depreciated_value(12000.0, 2024, 9)
    result = calculate_schedule_e(
        [RenditionLineItem(schedule="E", subsection="furniture_fixtures", historical_cost=12000.0, year_acquired=2024)]
    )
    assert result["subsection_totals"]["furniture_fixtures"] == expected_value


def test_schedule_e_machinery_uses_9_year_table():
    expected_value, _expected_factor = get_depreciated_value(32000.0, 2023, 9)
    result = calculate_schedule_e(
        [RenditionLineItem(schedule="E", subsection="machinery_equipment", historical_cost=32000.0, year_acquired=2023)]
    )
    assert result["subsection_totals"]["machinery_equipment"] == expected_value


def test_schedule_e_office_uses_8_year_table():
    expected_value, _expected_factor = get_depreciated_value(6400.0, 2024, 8)
    result = calculate_schedule_e(
        [RenditionLineItem(schedule="E", subsection="office_equipment", historical_cost=6400.0, year_acquired=2024)]
    )
    assert result["subsection_totals"]["office_equipment"] == expected_value


def test_schedule_e_computer_uses_5_year_table():
    expected_value, _expected_factor = get_depreciated_value(10000.0, 2024, 5)
    result = calculate_schedule_e(
        [RenditionLineItem(schedule="E", subsection="computer_equipment", historical_cost=10000.0, year_acquired=2024)]
    )
    assert result["subsection_totals"]["computer_equipment"] == expected_value


def test_schedule_e_pos_servers_uses_5_year_table():
    expected_value, _expected_factor = get_depreciated_value(15000.0, 2023, 5)
    result = calculate_schedule_e(
        [RenditionLineItem(schedule="E", subsection="pos_servers_mainframes", historical_cost=15000.0, year_acquired=2023)]
    )
    assert result["subsection_totals"]["pos_servers_mainframes"] == expected_value


def test_schedule_e_other_uses_9_year_table():
    expected_value, _expected_factor = get_depreciated_value(9000.0, 2022, 9)
    result = calculate_schedule_e(
        [RenditionLineItem(schedule="E", subsection="other", historical_cost=9000.0, year_acquired=2022)]
    )
    assert result["subsection_totals"]["other"] == expected_value


def test_values_over_trust_threshold_are_zeroed_and_flagged():
    result = calculate_schedule_b(
        [RenditionLineItem(schedule="B", exact_value=25000001.0, raw_text="bad OCR row")]
    )

    assert result["total"] == 0.0
    assert "value_over_trust_threshold" in result["flags"]
    assert result["evaluated_items"][0]["calculated_value"] == 0.0
    assert result["evaluated_items"][0]["value_source"] == "value_zeroed_over_trust_threshold"


def test_missing_year_creates_flag_and_does_not_guess():
    result = calculate_schedule_a(
        [RenditionLineItem(schedule="A", historical_cost=18000.0, raw_text="row with no year")]
    )

    assert result["total"] == 0.0
    assert "missing_year" in result["flags"]
    assert result["evaluated_items"][0]["calculated_value"] is None


def test_schedule_f_is_ignored():
    result = calculate_rendition_value(
        {
            "extracted_line_items": [
                {
                    "schedule": "F",
                    "good_faith_value": 999999.0,
                    "raw_text": "schedule f row",
                },
                {
                    "schedule": "B",
                    "exact_value": 2500.0,
                    "raw_text": "inventory row",
                },
            ]
        }
    )

    assert result["final_recommended_value"] == 2500.0
    assert result["schedule_totals"]["B"] == 2500.0
    assert result["schedule_totals"]["E"] == 0.0
    assert "schedule_f_ignored" in result["flags"]


def test_schedule_e_does_not_use_text_fallback_when_word_geometry_exists():
    result = calculate_rendition_value(
        {
            "pages": [
                {
                    "page_number": 1,
                    "text": (
                        "SCHEDULE E\n"
                        "Furniture and Fixtures\n"
                        "2025 132,500 119,250\n"
                        "TOTAL: 132,500 119,250\n"
                    ),
                    "ocr_blocks": [
                        {"text": "2025", "x0": 100, "top": 200},
                        {"text": "132,500", "x0": 150, "top": 200},
                        {"text": "119,250", "x0": 220, "top": 200},
                    ],
                }
            ],
            "metadata": {"tax_year": 2026},
        }
    )

    assert result["final_recommended_value"] == 119250.0
    assert len(result["line_items"]) == 1
    assert result["line_items"][0]["good_faith_value"] == 119250.0
