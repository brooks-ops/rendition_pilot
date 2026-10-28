"""Service-only, explicit import of a bounded captured source set.

python -m app.county_clerk.import_captured --captures PATH --district-id UUID
  --start YYYY-MM-DD --end YYYY-MM-DD --env-file PATH [--commit]
No network requests to EagleWeb. Original captures are archived privately.
"""
import argparse,hashlib,json,re,uuid
from datetime import date,datetime,timezone
from pathlib import Path
from . import service
from .captured import parse_detail
from .detailed import classify_detail
from app.comptroller.jurisdictions import get_jurisdiction_by_district_id

BUCKET='county-clerk-records'
def prepare(captures,source,start,end):
    date.fromisoformat(start);date.fromisoformat(end)
    if start>end:raise ValueError('Invalid date window')
    if source['county']!='LUBBOCK' or source['state']!='TX':raise ValueError('This captured format is verified only for Lubbock')
    index=json.loads((captures/'index.json').read_text());entries=index['records']
    count=re.search(r'([\d,]+) items found, displaying all items',index['text'])
    if not count or int(count[1].replace(',',''))!=len(entries) or not 1<=len(entries)<=30:raise ValueError('Captured index is incomplete or outside the bounded 30-record limit')
    originals={'index':index,'details':[]};records=[];identities=set()
    for entry in entries:
        number=entry['description'].strip().rsplit('\n',1)[-1]
        if not re.fullmatch(r'\d{10}',number):raise ValueError('Invalid document number')
        capture=json.loads((captures/(number+'.json')).read_text())
        record=parse_detail(capture,entry)
        identity=(record['document_number'],record['filing_type'])
        if identity in identities:raise ValueError('Duplicate source identity')
        if not start<=record['recording_date']<=end:raise ValueError('Filing outside declared date window')
        identities.add(identity);originals['details'].append(capture)
        record.update(document_reference=record['source_url'],source_pages=[],raw_text=capture.get('text') or capture.get('body') or '',owner_text='; '.join(record['owners']))
        # Keep full owner list; joined display field is never used for detailed matching.
        record['owner_name']='; '.join(record['owners']) or None
        records.append(record)
    return records,json.dumps(originals,ensure_ascii=False).encode()

def ingest(captures,district_id,start,end,*,commit=False):
    source=service.source_for(district_id);j=get_jurisdiction_by_district_id(district_id)
    if not j or j.id!=source['jurisdiction_id']:raise ValueError('District/source mismatch')
    records,original=prepare(captures,source,start,end)
    context=service.match_context(source,[{'assumed_name':r['assumed_name'],'owner_name':owner} for r in records for owner in r['owners'] or [None]])
    for r in records:r['intelligence']=classify_detail(r,context,j)
    if not commit:return {'records':len(records),'commit':False}
    identifier=str(uuid.uuid4());path=district_id+'/'+identifier+'.json'
    row=dict(id=identifier,source_id=source['id'],jurisdiction_id=j.id,district_id=district_id,filename='eagleweb-captured-records.json',sha256=hashlib.sha256(original).hexdigest(),storage_path=path,parser_version='eagleweb-captured-v1',status='PROCESSING',coverage='COMPLETE',page_count=0,reported_total=len(records),extracted_count=len(records),search_start=start,search_end=end,metadata={'storage_bucket':BUCKET,'collection_method':'bounded_browser_assisted','unattended':False})
    service.request('POST','county_clerk_dba_imports',payload=row)
    try:
        service.storage('POST',path,original,bucket=BUCKET,content_type='application/json')
        return service.request('POST','rpc/dba_commit_import',payload={'p_import_id':identifier,'p_filings':records})
    except Exception:
        current=service.request('GET','county_clerk_dba_imports',params={'select':'id,status,imported_count,duplicate_count','id':'eq.'+identifier,'district_id':'eq.'+district_id,'limit':1})
        if current and current[0]['status'] in ('COMPLETED','PARTIAL'):return current[0]
        service.request('PATCH','county_clerk_dba_imports',params={'id':'eq.'+identifier,'status':'eq.PROCESSING'},payload={'status':'FAILED','error_message':'Captured record import failed; retry safely using filing deduplication.','completed_at':datetime.now(timezone.utc).isoformat()})
        raise

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--captures',type=Path,required=True);p.add_argument('--district-id',required=True);p.add_argument('--start',required=True);p.add_argument('--end',required=True);p.add_argument('--env-file');p.add_argument('--commit',action='store_true');args=p.parse_args()
    if args.env_file:
        from dotenv import load_dotenv
        load_dotenv(args.env_file,override=True)
    print(json.dumps(ingest(args.captures,args.district_id,args.start,args.end,commit=args.commit)))
