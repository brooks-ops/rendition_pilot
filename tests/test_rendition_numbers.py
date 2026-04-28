from app.pipeline import _azure_analyze_result_to_pages, _google_document_ai_result_to_pages, _needs_ocr_fallback, _parse_money
from app.assessment_summary import AssessmentSummaryBuilder
from app.rendition_value_engine import calculate_rendition_value
from app.targeted_parser import TargetedRenditionParser


def test_parse_money_keeps_cents_and_repairs_split_leading_digit():
    assert _parse_money("$ 1 84,724.43") == 184724.43
    assert _parse_money("$ 9,000.00") == 9000.0
    assert _parse_money("34,798.73") == 34798.73
    assert _parse_money("6 45,442") == 45442.0


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


def test_google_document_ai_result_to_pages_preserves_lines_and_tokens():
    payload = {
        "document": {
            "text": "Total Fixed Assets $ 184,724.43\n",
            "pages": [
                {
                    "pageNumber": 1,
                    "dimension": {"width": 1000, "height": 2000},
                        "lines": [
                            {
                                "layout": {
                                    "textAnchor": {"textSegments": [{"startIndex": "0", "endIndex": "31"}]},
                                }
                            }
                        ],
                        "tokens": [
                            {
                                "layout": {
                                    "textAnchor": {"textSegments": [{"startIndex": "20", "endIndex": "31"}]},
                                    "boundingPoly": {
                                        "vertices": [
                                        {"x": 10, "y": 20},
                                        {"x": 70, "y": 20},
                                        {"x": 70, "y": 30},
                                        {"x": 10, "y": 30},
                                    ]
                                },
                                "confidence": 0.91,
                            }
                        }
                    ],
                }
            ],
        }
    }

    pages = _google_document_ai_result_to_pages(payload)

    assert pages[0]["text"] == "Total Fixed Assets $ 184,724.43"
    assert pages[0]["ocr_blocks"][0]["text"] == "184,724.43"
    assert pages[0]["text_source"] == "google_document_ai"


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


def test_schedule_e_subsection_parser_keeps_lower_top_section_rows_in_same_header_group():
    words = [
        {"text": "Furniture", "x0": 262, "top": 272},
        {"text": "and", "x0": 345, "top": 272},
        {"text": "Fixtures", "x0": 381, "top": 272},
        {"text": "Machinery", "x0": 794, "top": 272},
        {"text": "and", "x0": 888, "top": 272},
        {"text": "Equipment", "x0": 924, "top": 272},
        {"text": "Office", "x0": 1382, "top": 271},
        {"text": "Equipment", "x0": 1436, "top": 272},
        {"text": "Computer", "x0": 226, "top": 1122},
        {"text": "Equipment", "x0": 315, "top": 1122},
        {"text": "Other", "x0": 1158, "top": 1119},
        {"text": "2016", "x0": 108, "top": 823},
        {"text": "45,442", "x0": 209, "top": 820},
    ]

    rows = TargetedRenditionParser().parse_schedule_e_subsection_rows(words)

    assert rows[0]["subsection"] == "furniture_fixtures"
    assert rows[0]["historical_cost"] == 45442.0


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


def test_ambiguous_schedule_e_mapping_disables_rule_engine_value():
    valuation = calculate_rendition_value(
        {
            "metadata": {"tax_year": 2026},
            "line_items": [
                {
                    "schedule": "E",
                    "subsection": "computer_equipment",
                    "year_acquired": 2025,
                    "historical_cost": 161656.0,
                    "source_page": 3,
                    "confidence": 0.86,
                },
                {
                    "schedule": "E",
                    "subsection": "computer_equipment",
                    "year_acquired": 2024,
                    "historical_cost": 8395.0,
                    "source_page": 3,
                    "confidence": 0.86,
                },
                {
                    "schedule": "E",
                    "subsection": "computer_equipment",
                    "year_acquired": 2016,
                    "historical_cost": 45442.0,
                    "source_page": 3,
                    "confidence": 0.86,
                },
            ],
        }
    )

    assert "ambiguous_schedule_e_subsection_mapping" in valuation["flags"]
    assert valuation["final_recommended_value"] is None


def test_assessment_summary_falls_back_to_schedule_e_total_when_rule_engine_is_ambiguous():
    summary = AssessmentSummaryBuilder().build_summary(
        {
            "form_flags": {"signature_block_detected": True, "section_5_present": True},
            "schedule_e": {"total": 45442.0},
            "attachments": {"best_attachment_total": None},
            "review_flags": {},
            "ocr_reconciliation": {},
            "recommended_value": 184292.65,
            "recommended_value_source": "schedule_rule_engine",
            "valuation_flags": ["ambiguous_schedule_e_subsection_mapping"],
            "rendition_valuation": {"confidence": "high"},
        }
    )

    assert summary["recommended_value"] == 45442.0
    assert summary["recommended_path"] == "use_schedule_total_pending_review"
