from app.rendition_schema import (
    apply_rendition_valuation_rules,
    get_google_document_ai_processor_name,
    parse_text_to_rendition_schema,
    process_uploaded_rendition,
    should_use_document_ai,
)


def test_should_use_document_ai_for_short_or_unusable_text():
    assert should_use_document_ai("", {"score": 0.0, "usable": False, "missing_schedules": ["A"]}) is True
    assert should_use_document_ai("too short", {"score": 0.9, "usable": True, "missing_schedules": []}) is True


def test_parse_text_to_schema_and_apply_rules_for_inventory_and_supplies():
    text = """
    SCHEDULE B
    inventory line 18,250
    SCHEDULE C
    supplies line 725
    """

    schema = parse_text_to_rendition_schema(text)
    valuation = apply_rendition_valuation_rules(schema)

    assert schema["schedule_b"]["inventory_values"] == [18250.0]
    assert schema["schedule_c"]["supplies_values"] == [725.0]
    assert valuation["schedule_breakdown"]["schedule_b"]["total"] == 18250.0
    assert valuation["schedule_breakdown"]["schedule_c"]["total"] == 725.0


def test_process_uploaded_rendition_prefers_embedded_text_when_quality_is_good(monkeypatch):
    embedded_text = """
    SCHEDULE A
    fixture row 15,000 2024
    SCHEDULE B
    inventory row 18,250
    SCHEDULE C
    supplies row 725
    SCHEDULE D
    truck row 25,000 2022
    SCHEDULE E
    Furniture and Fixtures
    2024 12,000 9,500
    """

    monkeypatch.setattr(
        "app.rendition_schema.extract_pdf_text",
        lambda file: {
            "text": embedded_text,
            "pages": [{"page_number": 1, "text": embedded_text, "ocr_blocks": []}],
            "quality_score": 0.9,
            "usable": True,
            "quality_details": {
                "score": 0.9,
                "usable": True,
                "missing_schedules": [],
                "missing_columns": [],
                "table_columns_unreadable": False,
            },
        },
    )
    monkeypatch.setattr(
        "app.rendition_schema.run_google_document_ai",
        lambda file: (_ for _ in ()).throw(AssertionError("Document AI should not be called")),
    )

    result = process_uploaded_rendition("clean.pdf")

    assert result["extraction_provider"] == "embedded_text"
    assert result["debug"]["document_ai_used"] is False
    assert result["recommended_value"] > 0


def test_process_uploaded_rendition_uses_document_ai_for_low_quality_text(monkeypatch):
    monkeypatch.setattr(
        "app.rendition_schema.extract_pdf_text",
        lambda file: {
            "text": "scan artifact",
            "pages": [{"page_number": 1, "text": "scan artifact", "ocr_blocks": []}],
            "quality_score": 0.1,
            "usable": False,
            "quality_details": {
                "score": 0.1,
                "usable": False,
                "missing_schedules": ["A", "B", "C", "D", "E"],
                "missing_columns": ["good faith"],
                "table_columns_unreadable": True,
            },
        },
    )
    monkeypatch.setattr(
        "app.rendition_schema.run_google_document_ai",
        lambda file: {
            "document": {
                "text": "SCHEDULE B\ninventory row 18,250\n",
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
                                    "textAnchor": {"textSegments": [{"startIndex": "25", "endIndex": "31"}]},
                                    "boundingPoly": {"vertices": [{"x": 10, "y": 20}, {"x": 70, "y": 20}, {"x": 70, "y": 30}, {"x": 10, "y": 30}]},
                                },
                                "confidence": 0.92,
                            }
                        ],
                    }
                ],
            },
            "tables": [],
            "form_fields": [],
            "layout": [],
        },
    )

    result = process_uploaded_rendition("scanned.pdf")

    assert result["extraction_provider"] == "google_document_ai"
    assert result["debug"]["document_ai_used"] is True
    assert result["schedule_breakdown"]["schedule_b"]["total"] == 18250.0


def test_process_uploaded_rendition_falls_back_when_document_ai_fails(monkeypatch):
    fallback_text = """
    SCHEDULE A
    fixture row 15,000 2024
    SCHEDULE B
    inventory row 18,250
    """

    monkeypatch.setattr(
        "app.rendition_schema.extract_pdf_text",
        lambda file: {
            "text": fallback_text,
            "pages": [{"page_number": 1, "text": fallback_text, "ocr_blocks": []}],
            "quality_score": 0.2,
            "usable": False,
            "quality_details": {
                "score": 0.2,
                "usable": False,
                "missing_schedules": ["C", "D", "E"],
                "missing_columns": ["good faith"],
                "table_columns_unreadable": True,
            },
        },
    )
    monkeypatch.setattr(
        "app.rendition_schema.run_google_document_ai",
        lambda file: (_ for _ in ()).throw(RuntimeError("service unavailable")),
    )

    result = process_uploaded_rendition("fallback.pdf")

    assert result["extraction_provider"] == "fallback_text"
    assert result["debug"]["document_ai_error"] is not None
    assert "document_ai_failed_fallback_used" in result["review_flags"]


def test_google_document_ai_processor_name_builds_from_parts(monkeypatch):
    monkeypatch.delenv("GOOGLE_DOCUMENT_AI_PROCESSOR_NAME", raising=False)
    monkeypatch.setenv("GOOGLE_DOCUMENT_AI_PROJECT_ID", "proj")
    monkeypatch.setenv("GOOGLE_DOCUMENT_AI_LOCATION", "us")
    monkeypatch.setenv("GOOGLE_DOCUMENT_AI_PROCESSOR_ID", "proc")

    assert get_google_document_ai_processor_name() == "projects/proj/locations/us/processors/proc"
