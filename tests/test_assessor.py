from app.assessment_summary import AssessmentSummaryBuilder


def test_assessment_summary_prefers_rendered_value_over_component_schedule_totals():
    result = {
        "form_flags": {
            "signature_block_detected": True,
            "section_5_present": True,
        },
        "schedule_e": {"total": 132500.0},
        "attachments": {"best_attachment_total": None},
        "review_flags": {},
        "ocr_reconciliation": {},
        "resolved_values": {
            "rendered_value": 357350.0,
        },
    }

    summary = AssessmentSummaryBuilder().build_summary(result)

    assert summary["recommended_value"] == 357350.0
    assert summary["value_source"] == "rendered_value"
    assert summary["recommended_path"] == "use_rendered_value_pending_review"
