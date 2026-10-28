from unittest.mock import Mock
from app.pipeline import run_rendition_pipeline
from app.rendition_schema import process_uploaded_rendition

BLANK = '''Business Personal Property Rendition
Account Number P341728
Tax year 2026
Schedule A
Good Faith Estimate: __________
Property address: 4102 MAIN STREET
LUBBOCK TX 79401
Phone 806-555-1212
Schedule E
Historical Cost: __________
'''


def test_blank_value_fields_do_not_turn_address_into_4102(monkeypatch):
    pages = [{'page_number': 1, 'text': BLANK, 'ocr_blocks': [], 'text_source': 'pymupdf_tesseract_ocr'}]
    monkeypatch.setattr('app.pipeline._extract_pdf_bundle', lambda *a, **kw: {'pages': pages, 'ocr_reconciliation': {}})
    result = run_rendition_pipeline('not-reread.pdf', fast_review=True)
    assert result['assessment_summary']['recommended_value'] is None
    assert result['assessment_summary']['recommended_path'] == 'manual_review'
    assert not result['extracted_line_items']


def test_fast_schema_reuses_ocr_and_never_calls_cloud_or_reextracts(monkeypatch):
    forbidden = Mock(side_effect=AssertionError('Duplicate/cloud extraction'))
    monkeypatch.setattr('app.rendition_schema.extract_pdf_text', forbidden)
    monkeypatch.setattr('app.rendition_schema.run_google_document_ai', forbidden)
    pages = [{'page_number': 1, 'text': 'Schedule B\nInventory $4,102.00', 'ocr_blocks': []}]
    result = process_uploaded_rendition('not-reread.pdf', pages=pages, fast_review=True)
    assert result['recommended_value'] == 4102
    forbidden.assert_not_called()


def test_explicit_zero_remains_a_reported_value():
    from app.rendition_schema import build_empty_rendition_schema, apply_rendition_valuation_rules
    schema = build_empty_rendition_schema()
    schema['schedule_a']['good_faith_values'] = [0.0]
    assert apply_rendition_valuation_rules(schema)['recommended_value'] == 0.0
