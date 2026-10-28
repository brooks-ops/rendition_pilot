"""Evidence, source boundaries and recovery tests for manual Clerk imports."""
from pathlib import Path
from types import SimpleNamespace
import base64,os
import pytest
from fastapi import FastAPI,HTTPException
from fastapi.testclient import TestClient
from app.county_clerk import pdf_import,service
from app.county_clerk.api import router
from app.county_clerk.connector import EagleWebConnector,RetrievalNotAuthorized,next_window
URL='https://erecord.lubbockcounty.gov/recorder/web/splash.jsp'

def record(**values):
    return dict(assumed_name='Lone Wolf Garage',document_number='2026700001',filing_type='ASSUMED NAME',owner_name=None,owner_name_complete=False,**values)
def jurisdiction():return SimpleNamespace(account_type_prefixes={'personal':'P','real':'R'})
def account(id='a',number='P1',**values):return dict(id=id,real_account_number=number,business_name='Lone Wolf Garage',owner_name=None,dba_name=None,tax_year=2026,**values)
def test_actual_pdf():
    path=os.getenv('RPD_DBA_SAMPLE_PDF')
    if not path:pytest.skip('Set RPD_DBA_SAMPLE_PDF to the supplied printable PDF')
    p=pdf_import.parse_pdf(Path(path).read_bytes(),source_url=URL)
    assert (p['record_count'],p['reported_total'],p['page_count'],p['coverage'])==(100,1237,8,'INCOMPLETE')
    assert (p['search_start'],p['search_end'])==('2026-01-01','2026-12-31')
    assert p['records'][0]['document_number']=='2026700001'
    assert p['records'][-1]['document_number']=='2026700100'
    assert p['records'][2]['filing_type']=='ASSUMED NAME ABANDONMENT'
    assert all(not r['owner_name_complete'] and r['owner_name'] is None and r['document_reference'] for r in p['records'])
    assert all(r['business_address'] is None and r['mailing_address'] is None for r in p['records'])
    with pytest.raises(pdf_import.ImportError,match='different county'):
        pdf_import.parse_pdf(Path(path).read_bytes(),source_url='https://other.example/recorder/web/splash.jsp')

def test_name_only_match_never_high():
    r=service.classify(record(),{'accounts':[account()]},jurisdiction())
    assert r['classification']=='DBA_EXISTING_BUSINESS' and r['confidence']=='MEDIUM'
    assert r['matched_account_number']=='P1'
    assert not r['evidence']['owner_text_used_for_matching']
def test_annual_duplicates_and_real_accounts():
    r=service.classify(record(),{'accounts':[account(),{**account(id='old'),'tax_year':2025},account(id='land',number='R1')]},jurisdiction())
    assert len(r['evidence']['account_candidates'])==1
    assert r['matched_record_id']=='a'
def test_ambiguous_and_abandonment_are_leads():
    r=record();r['filing_type']='ASSUMED NAME ABANDONMENT'
    out=service.classify(r,{'accounts':[account(),account(id='b',number='P2')]},jurisdiction())
    assert out['classification']=='DBA_POTENTIAL_CLOSURE_LEAD'
    assert out['matched_record_id'] is None and out['is_ambiguous']
    assert 'does not prove' in out['match_reason']
def test_clipped_owner_and_sales_tax_never_supply_address():
    out=service.classify({**record(),'owner_text':'OLSO'}, {'accounts':[{**account(),'business_name':'OLSO'}],'sales_tax':[{'location_name':'Lone Wolf Garage','address':'123 Elsewhere'}]},jurisdiction())
    assert out['classification']=='DBA_NEEDS_REVIEW' and out['matched_record_id'] is None
    assert out['evidence']['filing'].get('business_address') is None
    assert out['evidence']['sales_tax_candidates'][0]['address']=='123 Elsewhere'
def test_original_is_district_scoped(monkeypatch):
    calls=[]
    def read(*args,**kwargs):calls.append(kwargs['params']);return []
    monkeypatch.setattr(service,'request',read)
    monkeypatch.setattr(service,'storage',lambda *a:pytest.fail('Must not read a foreign object'))
    with pytest.raises(pdf_import.ImportError):service.original('district-a','foreign-import')
    assert calls[0]['district_id']=='eq.district-a'
def test_preview_and_commit_guards_write_nothing(monkeypatch):
    monkeypatch.setattr(service,'source_for',lambda d:{'id':'s'})
    monkeypatch.setattr(service,'preview',lambda *a:{'sha256':'a'*64,'records':[{}],'coverage':'INCOMPLETE'})
    monkeypatch.setattr(service,'request',lambda *a,**k:pytest.fail('Guard must run before storage writes'))
    context=SimpleNamespace(district_id='d')
    with pytest.raises(pdf_import.ImportError,match='changed'):service.commit(context,b'x','x.pdf','b'*64,True)
    with pytest.raises(pdf_import.ImportError,match='Acknowledge'):service.commit(context,b'x','x.pdf','a'*64,False)
def test_api_auth_precedes_pdf_decode(monkeypatch):
    def denied(token):raise HTTPException(403,'No BPP access')
    app=FastAPI();app.include_router(router(denied));c=TestClient(app)
    monkeypatch.setattr(service,'preview',lambda *a:pytest.fail('Unauthorized preview'))
    assert c.post('/api/county-clerk/preview',json={'access_token':'bad','filename':'x','file_base64':'invalid'}).status_code==403
    for path in ['history','reviewers']:
        assert c.post('/api/county-clerk/'+path,json={'access_token':'bad'}).status_code==403
    assert c.post('/api/county-clerk/original',json={'access_token':'bad','import_id':'00000000-0000-0000-0000-000000000001'}).status_code==403

def test_connector_cannot_collect():
    connector=EagleWebConnector({'source_url':URL})
    assert connector.record_reference('https://evil.example/recorder/viewDoc.jsp?node=x') is None
    with pytest.raises(RetrievalNotAuthorized):connector.retrieve_window(None,None)

def test_corrupt_and_oversized_input():
    with pytest.raises(pdf_import.ImportError):pdf_import.parse_pdf(b'not a PDF',source_url=URL)
    with pytest.raises(pdf_import.ImportError):pdf_import.parse_pdf(b'%PDF-broken',source_url=URL)
    with pytest.raises(pdf_import.ImportError,match='25 MB'):pdf_import.parse_pdf(b'x'*(pdf_import.MAX_BYTES+1),source_url=URL)

class TextPage:
    rect=SimpleNamespace(height=800,width=600)
    def __init__(self,text):self.text=text
    def get_text(self,kind=None,**kwargs):return [] if kind=='words' else self.text
    def get_links(self):return []
class TextDocument(list):
    needs_pass=False
    def close(self):pass

def parse_text(monkeypatch,*pages):
    monkeypatch.setattr(pdf_import.fitz,'open',lambda **kw:TextDocument(TextPage(t) for t in pages))
    return pdf_import.parse_pdf(b'%PDF-test',source_url=URL)
def row(number='2027700001',name='THE LONG BUSINESS NAME',date='02/18/2027',owner='FULL OWNER NAME'):
    return f'ASSUMED NAME\n{number}\nRecording Date: {date} 10:20:06 AM\nAssumed Name: {name}\nOwner(s): {owner}\nExpires: 02/18/2037\n'
def test_varying_ranges_and_wrapped_record_across_pages(monkeypatch):
    p=parse_text(monkeypatch,URL+'\nRecDateID Feb 1 00:00:00 CST 2027 Feb 28 23:59:59 CST 2027\n'+row(name='THE LONG\nBUSINESS NAME').split('Owner(s)')[0], 'Owner(s): FULL OWNER NAME\nExpires: 02/18/2037\n1 items found, displaying 1 to 1')
    assert p['coverage']=='COMPLETE' and p['page_count']==2
    r=p['records'][0]
    assert r['assumed_name']=='THE LONG BUSINESS NAME' and r['owner_name']=='FULL OWNER NAME'
    assert r['source_pages']==[1,2] and r['recording_date']=='2027-02-18'
    assert p['search_start']=='2027-02-01' and p['search_end']=='2027-02-28'
def test_overlapping_windows_and_conflicting_duplicates(monkeypatch):
    p=parse_text(monkeypatch,URL+'\n'+row()+'1 items found, displaying 1 to 1',URL+'\n'+row()+'1 items found, displaying 1 to 1')
    assert p['record_count']==1 and p['coverage']=='COMPLETE'
    with pytest.raises(pdf_import.ImportError,match='Conflicting'):
        parse_text(monkeypatch,URL+'\n'+row()+row(name='DIFFERENT NAME'))
def test_unknown_source_and_missing_printed_pages(monkeypatch):
    p=parse_text(monkeypatch,row()+'1 items found, displaying 1 to 1')
    assert p['coverage']=='UNKNOWN'
    p=parse_text(monkeypatch,URL+'\n'+row()+'1 items found, displaying 1 to 1\n1/2')
    assert p['coverage']=='INCOMPLETE'
def test_invalid_date_is_rejected_not_guessed(monkeypatch):
    p=parse_text(monkeypatch,URL+'\n'+row(date='02/30/2027')+'1 items found, displaying 1 to 1')
    assert p['record_count']==0 and len(p['rejected_records'])==1 and p['coverage']=='INCOMPLETE'

def test_preview_scoped_to_authenticated_district(monkeypatch):
    seen=[]
    def preview(district,data):seen.append((district,data));return {'record_count':1}
    monkeypatch.setattr(service,'preview',preview)
    app=FastAPI();app.include_router(router(lambda token:SimpleNamespace(district_id='own-district')))
    r=TestClient(app).post('/api/county-clerk/preview',json={'access_token':'valid','filename':'x.pdf','file_base64':base64.b64encode(b'%PDF-test').decode(),'district_id':'foreign'})
    assert r.status_code==200 and seen==[('own-district',b'%PDF-test')]

def test_lost_rpc_response_does_not_overwrite_committed_import(monkeypatch):
    source={'id':'s','jurisdiction_id':'j','district_id':'d'}
    monkeypatch.setattr(service,'source_for',lambda *a:source)
    monkeypatch.setattr(service,'preview',lambda *a:{'sha256':'a'*64,'records':[record()],'coverage':'COMPLETE','parser_version':'test','page_count':1,'reported_total':1,'record_count':1,'search_start':None,'search_end':None,'warnings':[],'search_windows':[],'rejected_records':[],'notice':'Evidence only'})
    monkeypatch.setattr(service,'storage',lambda *a:None)
    monkeypatch.setattr(service,'get_jurisdiction_by_district_id',lambda d:SimpleNamespace(id='j'))
    monkeypatch.setattr(service,'match_context',lambda *a:{})
    monkeypatch.setattr(service,'classify',lambda *a:{})
    def request(method,table,**kw):
        if table=='rpc/dba_commit_import':raise service.requests.Timeout('lost response')
        if method=='GET':return [{'id':'done','status':'COMPLETED','imported_count':1,'duplicate_count':0}]
        assert method!='PATCH','Must not mark a completed transaction FAILED'
    monkeypatch.setattr(service,'request',request)
    result=service.commit(SimpleNamespace(district_id='d',user_id=None),b'%PDF-test','x.pdf','a'*64,False)
    assert result['status']=='COMPLETED' and result['imported_count']==1
