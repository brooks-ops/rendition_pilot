from unittest.mock import Mock
import pytest
from app.pipeline import run_rendition_pipeline
from app.assessment_summary import AssessmentSummaryBuilder


@pytest.mark.parametrize('schedule,label',[('A','Assets'),('B','Inventory'),('C','Supplies'),('D','Vehicles')])
@pytest.mark.parametrize('amount',['$0','$0.00','0','0.00'])
def test_reported_zero_survives_full_pipeline(monkeypatch,schedule,label,amount):
    pages=[{'page_number':1,'text':f'Business Personal Property Rendition\nAccount Number P341728\nSchedule {schedule}\n{label} {amount}','ocr_blocks':[]}]
    monkeypatch.setattr('app.pipeline._extract_pdf_bundle',lambda *a,**kw:{'pages':pages,'ocr_reconciliation':{}})
    result=run_rendition_pipeline('zero.pdf',fast_review=True)
    assert result['assessment_summary']['recommended_value']==0
    assert result['assessment_summary']['recommended_path']!='manual_review'


@pytest.mark.parametrize('field',['rendered_value','good_faith_value'])
def test_summary_does_not_replace_explicit_zero_with_fallback(field):
    result=AssessmentSummaryBuilder().build_summary({field:0,'resolved_values':{field:4102}})
    assert result['recommended_value']==0


def test_zero_schedule_totals_and_word_cells():
    from app.targeted_parser import TargetedRenditionParser
    p=TargetedRenditionParser()
    assert p._money_word_value({'text':'$0'})==0
    assert p._money_word_value({'text':'0.00'})==0
    assert p._money_word_value({'text':'O'}) is None
    assert p.parse_schedule_e_total('Schedule E\nTotal Market Value: $0.00')['schedule_e_total']==0
    assert p.parse_attachment_summary(['TOTAL FIXED ASSETS: $0'])['best_attachment_total']==0


def test_scanned_zero_reaches_review_response(tmp_path):
    import base64,shutil
    import fitz
    from backend.main import ReviewRunRequest,review_run
    if not shutil.which('tesseract'):pytest.skip('System Tesseract required')
    source=fitz.open();p=source.new_page()
    for y,line in enumerate(['Business Personal Property Rendition','Account Number P341728','Schedule B','Inventory $0.00']):
        p.insert_text((60,80+y*45),line,fontsize=18)
    scan=fitz.open();scan.new_page().insert_image(p.rect,stream=p.get_pixmap(dpi=200).tobytes('png'))
    response=review_run(ReviewRunRequest(file_name='zero-scan.pdf',file_base64=base64.b64encode(scan.tobytes()).decode(),fast_review=True))
    assert response['result']['assessment_summary']['recommended_value']==0
    source.close();scan.close()


@pytest.mark.parametrize('label',['Rendered Value','Good Faith Estimate','Schedule E\nTotal Market Value'])
def test_labeled_zero_total_reaches_summary(monkeypatch,label):
    pages=[{'page_number':1,'text':'Business Personal Property Rendition\n'+label+': $0','ocr_blocks':[]}]
    monkeypatch.setattr('app.pipeline._extract_pdf_bundle',lambda *a,**kw:{'pages':pages,'ocr_reconciliation':{}})
    assert run_rendition_pipeline('zero.pdf',fast_review=True)['assessment_summary']['recommended_value']==0
