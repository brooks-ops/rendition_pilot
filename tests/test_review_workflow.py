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
