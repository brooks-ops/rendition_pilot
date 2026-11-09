"""Account-folder returned-mail evidence. No account or mailing-address updates."""
from __future__ import annotations
import base64
import hashlib
import re
from io import BytesIO
from pathlib import PurePosixPath
from urllib.parse import quote

import requests
from app.comptroller.service import _request_json,get_supabase_config,postgrest_headers
from app.comptroller.jurisdictions import get_jurisdiction_by_district_id
from app.comptroller.property_adapter import get_property_adapter
from app.comptroller.property_matching import classify_account_type

BUCKET='returned-mail'
MAX_BYTES=12*1024*1024
REASONS={'UNKNOWN':'Needs review','UNABLE_TO_FORWARD':'Unable to forward','NOT_DELIVERABLE':'Not deliverable as addressed',
 'VACANT':'Vacant','REFUSED':'Refused','DECEASED':'Deceased','INSUFFICIENT_ADDRESS':'Insufficient address',
 'NO_SUCH_NUMBER':'No such number','ATTEMPTED_NOT_KNOWN':'Attempted—not known','UNCLAIMED':'Unclaimed','RETURN_TO_SENDER':'Return to sender','WRONG_ACCOUNT':'Filed under wrong account'}

class MailImportError(ValueError):pass

def request(method,table,*,params=None,payload=None):
    url,key=get_supabase_config()
    return _request_json(method,url+'/rest/v1/'+table,postgrest_headers(key,prefer='return=representation'),params=params,json_payload=payload)

def account_from_path(path):
    parts=path.replace('\\','/').split('/')
    if len(parts)<2 or any(p in ('','..','.') for p in parts) or path.startswith(('/', '\\')) or re.match(r'^[A-Za-z]:',path):
        raise MailImportError('Select the account-number folder or its parent folder; standalone files need an account folder.')
    account=parts[-2].strip().upper()
    if not re.fullmatch(r'[A-Z]?\d{1,20}',account):
        raise MailImportError('The file must be directly inside a folder named with its exact account number, such as P123456.')
    return account,PurePosixPath(path.replace('\\','/')).name

def extract_scan(data,filename):
    """Bounded local OCR, preserving originals even when OCR is unavailable."""
    import fitz
    from PIL import Image,ImageOps
    texts=[];errors=[];pages=0
    def ocr(image):
        from app.pipeline import _configure_tesseract
        import pytesseract
        if not _configure_tesseract():raise RuntimeError('OCR unavailable')
        return pytesseract.image_to_string(image,lang='eng',config='--psm 6',timeout=20)
    if data.startswith(b'%PDF-'):
        content_type='application/pdf';extension='pdf'
        try:
            with fitz.open(stream=data,filetype='pdf') as doc:
                if doc.needs_pass or not 1<=len(doc)<=10:raise MailImportError('Use an unencrypted PDF with 1–10 pages.')
                pages=len(doc)
                for page in doc:
                    text=page.get_text()[:50000]
                    if len(text.strip())<40:
                        try:
                            if page.rect.width<=0 or page.rect.height<=0:raise ValueError('Invalid page')
                            scale=min(2.5,(4_000_000/(page.rect.width*page.rect.height))**0.5)
                            pix=page.get_pixmap(matrix=fitz.Matrix(scale,scale),colorspace=fitz.csRGB,alpha=False)
                            with Image.frombytes('RGB',[pix.width,pix.height],pix.samples) as image:text=ocr(image)
                        except Exception:errors.append('A page could not be read automatically; review its scan.')
                    texts.append(text[:50000])
        except MailImportError:raise
        except Exception as exc:raise MailImportError('This PDF could not be opened.') from exc
    else:
        try:
            with Image.open(BytesIO(data)) as source:
                if source.format not in ('JPEG','PNG') or source.width*source.height>20_000_000 or getattr(source,'n_frames',1)!=1:
                    raise MailImportError('Use a JPEG/PNG image up to 20 megapixels, or a PDF.')
                extension='jpg' if source.format=='JPEG' else 'png';content_type='image/jpeg' if extension=='jpg' else 'image/png';pages=1
                with ImageOps.exif_transpose(source).convert('RGB') as image:
                    image.thumbnail((2400,2400))
                    try:texts.append(ocr(image)[:50000])
                    except Exception:errors.append('OCR unavailable; review the original scan.')
        except MailImportError:raise
        except Exception as exc:raise MailImportError('Use a valid PDF, JPEG or PNG scan.') from exc
    text='\n\n'.join(texts)
    reason='UNKNOWN'
    patterns=[('UNABLE_TO_FORWARD',r'UNABLE\s+TO\s+FORWARD'),('NOT_DELIVERABLE',r'NOT\s+DELIVERABLE\s+AS\s+ADDRESSED'),
      ('ATTEMPTED_NOT_KNOWN',r'ATTEMPTED\s*[-–]?\s*NOT\s+KNOWN'),('INSUFFICIENT_ADDRESS',r'INSUFFICIENT\s+ADDRESS'),
      ('NO_SUCH_NUMBER',r'NO\s+SUCH\s+NUMBER'),('VACANT',r'\bVACANT\b'),('REFUSED',r'\bREFUSED\b'),
      ('DECEASED',r'\bDECEASED\b'),('UNCLAIMED',r'\bUNCLAIMED\b'),('RETURN_TO_SENDER',r'RETURN\s+TO\s+SENDER')]
    for code,pattern in patterns:
        if re.search(pattern,text,re.I):reason=code;break
    forward=None
    # Only retain explicitly labeled text; never guess a replacement from
    # the sender/recipient blocks on an envelope.
    lines=text.splitlines()
    for i,line in enumerate(lines):
        if re.search(r'(?:FORWARDING ADDRESS|NOTIFY SENDER OF NEW ADDRESS|NEW ADDRESS)\s*:',line,re.I):
            tail=line.split(':',1)[1].strip()
            forward='\n'.join(([tail] if tail else [])+[x.strip() for x in lines[i+1:i+4] if x.strip()])[:1000] or None
            break
    return dict(content_type=content_type,extension=extension,page_count=pages,ocr_text=text,
        ocr_status='PARTIAL' if errors and text.strip() else 'UNAVAILABLE' if errors else 'READ' if text.strip() else 'UNREADABLE',
        ocr_warnings=errors,suggested_reason=reason,suggested_forwarding=forward,
        printed_accounts=sorted(set(re.findall(r'\bP\d{3,20}\b',text.upper()))))

def storage(method,path,*,data=None,content_type=None):
    url,key=get_supabase_config()
    response=requests.request(method,url+'/storage/v1/object/'+BUCKET+'/'+quote(path,safe='/'),
        headers={'apikey':key,'Authorization':'Bearer '+key,'Content-Type':content_type or 'application/octet-stream',
                 'x-upsert':'true'},data=data,timeout=60)
    response.raise_for_status();return response

def import_scan(context,relative_path,file_base64):
    account,filename=account_from_path(relative_path)
    if len(file_base64)>MAX_BYTES*4//3+4:raise MailImportError('Each scan must be 12 MB or smaller.')
    try:data=base64.b64decode(file_base64,validate=True)
    except Exception as exc:raise MailImportError('The scan upload could not be decoded.') from exc
    if not data or len(data)>MAX_BYTES:raise MailImportError('Each scan must contain data and be 12 MB or smaller.')
    jurisdiction=get_jurisdiction_by_district_id(context.district_id)
    if not jurisdiction:raise MailImportError('This district has no configured account source.')
    records=get_property_adapter(jurisdiction).find_properties_by_real_account(jurisdiction,account)
    # An active-name discovery export is not the returned-mail account universe.
    # Keep the exact imported BPP-account requirement, including ambiguity guards.
    records=[p for p in records if classify_account_type(p.real_account_number,prefixes=jurisdiction.account_type_prefixes)=='PERSONAL']
    if len(records)!=1:raise MailImportError('The account number does not identify one imported BPP account. Check its number and CAD import before retrying.')
    prop=records[0];sha=hashlib.sha256(data).hexdigest()
    previous=request('GET','returned_mail_documents',params={'select':'id,intelligence_item_id','district_id':'eq.'+context.district_id,
        'account_number':'eq.'+account,'sha256':'eq.'+sha,'limit':1})
    if previous:return {'duplicate':True,'document_id':previous[0]['id'],'item_id':previous[0]['intelligence_item_id'],'account':account}
    extracted=extract_scan(data,filename)
    if (prop.account_status or '').strip().upper()=='NOT_IN_ACTIVE_DBA_EXPORT':
        extracted['ocr_warnings'].append('This imported BPP account is absent from the active DBA export. Verify its current status in CRS; absence does not establish closure.')
    path=context.district_id+'/'+account+'/'+sha+'.'+extracted.pop('extension')
    storage('POST',path,data=data,content_type=extracted['content_type'])
    mismatch=bool(extracted['printed_accounts'] and extracted['printed_accounts']!=[account])
    payload=dict(account_number=account,filename=filename,relative_path=relative_path,sha256=sha,storage_path=path,
        byte_count=len(data),matched_record_id=prop.property_id,owner_name=prop.owner_name,
        business_name=prop.business_name or prop.dba_name or prop.owner_name or account,
        crs_recipient=prop.mailing_recipient,crs_imported_at=prop.mailing_imported_at,crs_as_of_date=prop.mailing_source_as_of_date,crs_address='\n'.join(str(v) for v in (prop.mailing_address_line1,prop.mailing_address_line2,
            prop.mailing_unit,prop.mailing_city,prop.mailing_state,prop.mailing_zip) if v),account_mismatch=mismatch,**extracted)
    return request('POST','rpc/returned_mail_commit',payload={'p_district_id':context.district_id,'p_jurisdiction_id':jurisdiction.id,
        'p_actor_id':context.user_id,'p_document':payload})

def documents(context,account,offset=0):
    fields='id,intelligence_item_id,account_number,filename,relative_path,uploaded_at,ocr_status,ocr_warnings,suggested_reason,suggested_forwarding,account_mismatch,review_status,return_date,return_reason,forwarding_address,review_notes,reviewed_at,version'
    rows=request('GET','returned_mail_documents',params={'select':fields,'district_id':'eq.'+context.district_id,
        'account_number':'eq.'+account,'order':'uploaded_at.desc,id.desc','limit':20,'offset':offset})
    return {'documents':rows,'next_offset':offset+len(rows) if len(rows)==20 else None}


def document(context,document_id):
    rows=request('GET','returned_mail_documents',params={'select':'*','id':'eq.'+str(document_id),'district_id':'eq.'+context.district_id,'limit':1})
    if not rows:raise MailImportError('Returned-mail scan was not found in your district.')
    row=rows[0]
    expected=re.escape(context.district_id+'/'+row['account_number']+'/')+r'[0-9a-f]{64}\.(?:pdf|jpg|png)'
    if not re.fullmatch(expected,row['storage_path']):raise MailImportError('Scan storage reference is invalid.')
    return row
