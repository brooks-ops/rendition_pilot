import fitz

from app.review_workflow import stamp_reviewed_pdf, wrap_text


def test_wrap_text_splits_long_notes():
    lines = wrap_text("one two three four five six", max_chars=13)
    assert lines == ["one two three", "four five six"]


def test_stamp_reviewed_pdf_appends_review_notes_page(tmp_path, monkeypatch):
    monkeypatch.setattr("app.review_workflow.APPRAISER_UPLOAD_DIR", tmp_path)
    monkeypatch.setattr("app.review_workflow.OUTPUT_DIR", tmp_path)
    monkeypatch.setattr("app.review_workflow.COMPLETED_DIR", tmp_path)

    doc = fitz.open()
    doc.new_page(width=612, height=792)
    source_bytes = doc.tobytes()
    doc.close()

    out_path = stamp_reviewed_pdf(
        file_name="sample.pdf",
        file_bytes=source_bytes,
        final_record={
            "account_number": "P12345",
            "final_value": 123456,
            "final_source": "manual_override",
            "appraiser_initials": "BB",
            "decision": "adjusted",
            "locked_at": "2026-04-21T15:00:00",
            "appraiser_notes": "Use handwritten total from attached schedule after review.",
        },
    )

    stamped = fitz.open(out_path)
    try:
        assert len(stamped) == 2
        full_text = "\n".join(page.get_text() for page in stamped)
        assert "APPRAISER REVIEW NOTES" in full_text
        assert "Use handwritten total" in full_text
        assert "VALUE: $123,456.00" in full_text
    finally:
        stamped.close()


def test_stamp_reviewed_pdf_appends_calculator_summary_pages(tmp_path, monkeypatch):
    monkeypatch.setattr("app.review_workflow.APPRAISER_UPLOAD_DIR", tmp_path)
    monkeypatch.setattr("app.review_workflow.OUTPUT_DIR", tmp_path)
    monkeypatch.setattr("app.review_workflow.COMPLETED_DIR", tmp_path)

    doc = fitz.open()
    doc.new_page(width=612, height=792)
    source_bytes = doc.tobytes()
    doc.close()

    out_path = stamp_reviewed_pdf(
        file_name="sample.pdf",
        file_bytes=source_bytes,
        final_record={
            "account_number": "P12345",
            "final_value": 125000,
            "final_source": "calculator_combined_total",
            "appraiser_initials": "BB",
            "decision": "accepted",
            "locked_at": "2026-04-28T15:00:00",
            "calculated_total_value": 125000,
            "saved_calculators": [
                {
                    "name": "Schedule A - Machinery & Equipment",
                    "depreciation_table": "8_year",
                    "tax_year": 2026,
                    "section_total": 125000,
                    "rows": [
                        {"display_year": "2025", "cost": 100000, "factor": 0.75, "value": 75000},
                        {"display_year": "2024", "cost": 50000, "factor": 0.60, "value": 30000},
                    ],
                }
            ],
        },
    )

    stamped = fitz.open(out_path)
    try:
        assert len(stamped) == 2
        full_text = "\n".join(page.get_text() for page in stamped)
        assert "APPRAISER CALCULATOR WORKSHEET" in full_text
        assert "Schedule A - Machinery & Equipment" in full_text
        assert "Calculated Total Value: $125,000.00" in full_text
    finally:
        stamped.close()
