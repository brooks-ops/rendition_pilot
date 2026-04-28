from app.pipeline import _azure_analyze_result_to_pages, _needs_ocr_fallback, _parse_money
from app.targeted_parser import TargetedRenditionParser


def test_parse_money_keeps_cents_and_repairs_split_leading_digit():
    assert _parse_money("$ 1 84,724.43") == 184724.43
    assert _parse_money("$ 9,000.00") == 9000.0
    assert _parse_money("34,798.73") == 34798.73


def test_attachment_summary_uses_labeled_total_not_largest_bad_parse():
    text = """
    Lubbock Office Good Faith Estimate of Market Value
    Rheometer $ 4,500.00
    200 Cubic Ft. Ribbon Blender $ 9,000.00
    Ram 1500 2019 - 4x4 Crew Cab - OM $ 30,250.00
    16' Bumper Hitch Parts Trailer VIN...0467 $ 7,175.70
    34' Racing Trailer VIN...2882 $ 9,000.00
    Modified Trailer w/Blower S/N...2305 $ 34,798.73
    Excel PCX $ 90,000.00
    Total Fixed Assets $ 1 84,724.43
    """

    result = TargetedRenditionParser().parse_attachment_summary([text])

    assert result["attachment_summary_present"] is True
    assert result["best_attachment_total"] == 184724.43
    assert 9000000.0 not in result["attachment_total_candidates"]


def test_attachment_summary_prefers_rendered_value_total_from_summary_page():
    page_text = """
    Tax Obligation of Taxpayer - Personal Property
    Machinery and Equipment
    Summary by State Class and Age
    Reported Cost
    Current Value
    Rendered Value
    1 50.606.17 46.051.61 46,052.00
    2 41.945.00 34,814.35 34,814.00
    3 46,854.84 35,141.13 35,141.00
    4 15,521.81 10,554.83 10,555.00
    154,927.82 126,561.92 126,562.00
    """

    result = TargetedRenditionParser().parse_attachment_summary([page_text])

    assert result["attachment_summary_present"] is True
    assert result["best_attachment_total"] == 126562.0


def test_azure_analyze_result_to_pages_preserves_lines_and_words():
    payload = {
        "status": "succeeded",
        "analyzeResult": {
            "pages": [
                {
                    "pageNumber": 1,
                    "lines": [
                        {"content": "Total Fixed Assets $ 1 84,724.43"},
                    ],
                    "words": [
                        {
                            "content": "184,724.43",
                            "confidence": 0.91,
                            "polygon": [10, 20, 70, 20, 70, 30, 10, 30],
                        }
                    ],
                }
            ]
        },
    }

    pages = _azure_analyze_result_to_pages(payload)

    assert pages[0]["text"] == "Total Fixed Assets $ 1 84,724.43"
    assert pages[0]["ocr_blocks"][0]["text"] == "184,724.43"
    assert pages[0]["text_source"] == "azure_document_intelligence"


def test_schedule_e_row_parser_pairs_years_with_amounts_by_geometry():
    words = [
        {"text": "2025", "x0": 1219, "top": 385},
        {"text": "46,052", "x0": 1117, "top": 387},
        {"text": "2024", "x0": 1219, "top": 434},
        {"text": "34,814", "x0": 1119, "top": 433},
        {"text": "2023", "x0": 1219, "top": 482},
        {"text": "35,141", "x0": 1119, "top": 481},
        {"text": "2022", "x0": 1219, "top": 530},
        {"text": "10,555", "x0": 1121, "top": 531},
        {"text": "TOTAL", "x0": 1206, "top": 1050},
        {"text": "126,562", "x0": 1109, "top": 1052},
    ]

    rows = TargetedRenditionParser().parse_schedule_e_year_rows_from_words(words)

    assert [(row["year_acquired"], row["amount"]) for row in rows] == [
        (2025, 46052.0),
        (2024, 34814.0),
        (2023, 35141.0),
        (2022, 10555.0),
    ]


def test_parse_schedule_e_total_accepts_dotted_thousands_separator():
    text = "SCHEDULE E TOTAL: 126.562"

    result = TargetedRenditionParser().parse_schedule_e_total(text)

    assert result["schedule_e_present"] is True
    assert result["schedule_e_total"] == 126562.0


def test_schedule_e_subsection_parser_uses_visual_regions():
    words = [
        {"text": "2024", "x0": 100, "top": 200},
        {"text": "16,656", "x0": 150, "top": 200},
        {"text": "2023", "x0": 510, "top": 205},
        {"text": "8,395", "x0": 560, "top": 205},
        {"text": "2022", "x0": 920, "top": 210},
        {"text": "19,586", "x0": 980, "top": 210},
        {"text": "2021", "x0": 110, "top": 740},
        {"text": "51,573", "x0": 165, "top": 740},
        {"text": "2020", "x0": 525, "top": 748},
        {"text": "4,055", "x0": 580, "top": 748},
        {"text": "2019", "x0": 940, "top": 752},
        {"text": "45,442", "x0": 995, "top": 752},
    ]

    rows = TargetedRenditionParser().parse_schedule_e_subsection_rows(words)

    pairs = {(row["subsection"], row["year_acquired"], row["historical_cost"]) for row in rows}
    assert ("furniture_fixtures", 2024, 16656.0) in pairs
    assert ("machinery_equipment", 2023, 8395.0) in pairs
    assert ("office_equipment", 2022, 19586.0) in pairs
    assert ("computer_equipment", 2021, 51573.0) in pairs
    assert ("pos_servers_mainframes", 2020, 4055.0) in pairs
    assert ("other", 2019, 45442.0) in pairs


def test_schedule_e_subsection_totals_only_come_from_total_rows():
    words = [
        {"text": "2025", "x0": 100, "top": 200},
        {"text": "16,656", "x0": 150, "top": 200},
        {"text": "2025", "x0": 600, "top": 200},
        {"text": "2,000", "x0": 660, "top": 200},
        {"text": "2016", "x0": 100, "top": 480},
        {"text": "45,442", "x0": 150, "top": 480},
        {"text": "TOTAL:", "x0": 60, "top": 690},
        {"text": "89,091", "x0": 150, "top": 690},
        {"text": "TOTAL:", "x0": 300, "top": 690},
        {"text": "12,500", "x0": 360, "top": 690},
        {"text": "2025", "x0": 100, "top": 860},
        {"text": "1,000", "x0": 150, "top": 860},
    ]

    totals = TargetedRenditionParser().parse_schedule_e_subsection_totals(words)

    assert totals["furniture_fixtures"] == 89091.0
    assert totals["machinery_equipment"] == 12500.0
    assert 45442.0 not in totals.values()


def test_needs_ocr_fallback_detects_garbled_embedded_text():
    pages = [
        {
            "text": (
                "aJsiness Personal Proporty Ro.dition olTaxabl6 Prop€.ty "
                "SCHIDULE E Fumilure l\\rachinery 2o2A 2A2A 20r9 "
                "lfunder$20,000,compleleonlyschedul€Aandilapplicable"
            )
        }
    ]

    assert _needs_ocr_fallback(pages) is True
