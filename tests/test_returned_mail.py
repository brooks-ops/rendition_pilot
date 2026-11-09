import base64
from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock
import fitz
import pytest
from PIL import Image
from app import returned_mail as mail
from app.comptroller.property_adapter import _row_to_property

@pytest.mark.parametrize('path,account', [('Returned Mail/P123456/2026-11-09.pdf','P123456'),('P123456/front.jpg','P123456'),('root/000123/return.png','000123'),('root\\p123456\\back.png','P123456')])
def test_account_folder_path(path,account):assert mail.account_from_path(path)[0]==account

@pytest.mark.parametrize('path',['file.pdf','../P1/a.pdf','/P1/a.pdf','root/P1/../a.pdf','root/wrong/a.pdf','root/P1/subfolder/a.pdf'])
def test_invalid_paths_not_guessed(path):
    with pytest.raises(mail.MailImportError):mail.account_from_path(path)

def pdf(text):
    with fitz.open() as doc:
        page=doc.new_page();page.insert_text((30,50),text)
        return doc.tobytes()

def test_embedded_text_reasons_forwarding_and_accounts():
    result=mail.extract_scan(pdf('P123456\nRETURN TO SENDER\nUNABLE TO FORWARD\nNEW ADDRESS:\n123 Main St\nLubbock TX 79401\n'),'test.pdf')
    assert result['suggested_reason']=='UNABLE_TO_FORWARD'
    assert result['suggested_forwarding']=='123 Main St\nLubbock TX 79401'
    assert result['printed_accounts']==['P123456'] and result['ocr_status']=='READ'

def test_sender_address_is_not_guessed_as_forwarding():
    result=mail.extract_scan(pdf('RETURN TO SENDER\n100 Sender St\nLubbock TX 79401\nNot deliverable as addressed'),'test.pdf')
    assert result['suggested_forwarding'] is None

def test_ocr_failure_keeps_readable_file_for_manual_review(monkeypatch):
    import app.pipeline
    monkeypatch.setattr(app.pipeline,'_configure_tesseract',lambda:False)
    image=Image.new('RGB',(200,100),'white');out=BytesIO();image.save(out,format='PNG')
    result=mail.extract_scan(out.getvalue(),'image.png')
    assert result['ocr_status']=='UNAVAILABLE' and result['suggested_reason']=='UNKNOWN'

def test_pdf_page_bound_and_invalid_files():
    with fitz.open() as doc:
        for i in range(11):doc.new_page()
        with pytest.raises(mail.MailImportError):mail.extract_scan(doc.tobytes(),'big.pdf')
    with pytest.raises(mail.MailImportError):mail.extract_scan(b'<html>not a scan</html>','fake.pdf')

@pytest.fixture
def setup(monkeypatch):
    context=SimpleNamespace(district_id='district',user_id='actor')
    j=SimpleNamespace(id='jurisdiction',account_type_prefixes={'personal':'P','real':'R'})
    prop=_row_to_property(dict(id='record',jurisdiction_id=j.id,source_property_id='1',real_account_number='P123456',mailing_address_line1='100 Main St'))
    monkeypatch.setattr(mail,'get_jurisdiction_by_district_id',lambda _:j)
    adapter=SimpleNamespace(find_properties_by_real_account=Mock(return_value=[prop]))
    monkeypatch.setattr(mail,'get_property_adapter',lambda _:adapter)
    query=Mock(side_effect=lambda method,table,**kw:[] if method=='GET' else {'duplicate':False})
    monkeypatch.setattr(mail,'request',query)
    storage=Mock();monkeypatch.setattr(mail,'storage',storage)
    return context,adapter,query,storage

def test_import_exact_account_and_private_deterministic_path(setup):
    context,adapter,query,storage=setup
    result=mail.import_scan(context,'Root/P123456/envelope.pdf',base64.b64encode(pdf('RETURN TO SENDER\nUnable to forward - USPS returned envelope')).decode())
    assert result['duplicate'] is False
    adapter.find_properties_by_real_account.assert_called_once()
    assert storage.call_args.args[1].startswith('district/P123456/')
    payload=query.call_args.kwargs['payload']
    assert payload['p_district_id']=='district' and payload['p_document']['account_number']=='P123456'
    assert payload['p_document']['suggested_reason']=='UNABLE_TO_FORWARD'

def test_duplicate_does_not_upload_or_reopen(setup,monkeypatch):
    context,_,query,storage=setup
    query.return_value=[{'id':'doc','intelligence_item_id':'resolved-item'}];query.side_effect=None
    extract=Mock();monkeypatch.setattr(mail,'extract_scan',extract)
    result=mail.import_scan(context,'P123456/envelope.pdf',base64.b64encode(b'file').decode())
    assert result['duplicate'] and result['item_id']=='resolved-item'
    storage.assert_not_called();extract.assert_not_called()

def test_unknown_or_ambiguous_account_not_forced(setup):
    context,adapter,query,storage=setup
    adapter.find_properties_by_real_account.return_value=[]
    with pytest.raises(mail.MailImportError):mail.import_scan(context,'P123456/scan.pdf',base64.b64encode(b'file').decode())
    query.assert_not_called();storage.assert_not_called()

def test_absence_from_active_dba_export_does_not_block_exact_returned_mail_account(setup):
    context,adapter,query,storage=setup
    adapter.find_properties_by_real_account.return_value=[replace(adapter.find_properties_by_real_account.return_value[0],account_status='NOT_IN_ACTIVE_DBA_EXPORT')]
    mail.import_scan(context,'P123456/envelope.pdf',base64.b64encode(pdf('RETURN TO SENDER - returned envelope for staff review')).decode())
    assert storage.called
    warnings=query.call_args.kwargs['payload']['p_document']['ocr_warnings']
    assert any('Verify its current status in CRS' in warning for warning in warnings)

def test_multiple_imported_accounts_still_rejected(setup):
    context,adapter,query,storage=setup
    prop=adapter.find_properties_by_real_account.return_value[0]
    adapter.find_properties_by_real_account.return_value=[prop,replace(prop,property_id='other-record')]
    with pytest.raises(mail.MailImportError):mail.import_scan(context,'P123456/scan.pdf',base64.b64encode(b'file').decode())
    query.assert_not_called();storage.assert_not_called()

def test_invalid_or_oversized_payload_does_not_reach_storage(setup):
    context,_,_,storage=setup
    with pytest.raises(mail.MailImportError):mail.import_scan(context,'P123456/scan.pdf','bad!')
    with pytest.raises(mail.MailImportError):mail.import_scan(context,'P123456/scan.pdf','A'*(mail.MAX_BYTES*4//3+5))
    storage.assert_not_called()

def test_district_filter_and_path_check_on_download(setup):
    context,_,query,_=setup
    query.side_effect=None;query.return_value=[]
    with pytest.raises(mail.MailImportError):mail.document(context,'doc')
    assert query.call_args.kwargs['params']['district_id']=='eq.district'
    query.return_value=[{'account_number':'P123456','storage_path':'other/P123456/file.pdf'}]
    with pytest.raises(mail.MailImportError):mail.document(context,'doc')
