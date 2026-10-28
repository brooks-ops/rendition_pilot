"""Service-only, explicit import of a bounded captured source set.

python -m app.county_clerk.import_captured --captures PATH --district-id UUID
  --start YYYY-MM-DD --end YYYY-MM-DD --env-file PATH [--commit]
No network requests to EagleWeb. Original captures are archived privately.
"""
import argparse,hashlib,json,re,uuid
from concurrent.futures import ThreadPoolExecutor
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
    if not 1<=len(entries)<=3000:raise ValueError('Captured set is incomplete or exceeds 3,000 filings')
    pages=[]
    if index.get('pages',1)>1:
        expected=1
        for page_number in range(1,index['pages']+1):
            page=json.loads((captures/f'page-{page_number}.json').read_text())
            count=re.search(r'([\d,]+) items found, displaying ([\d,]+) to ([\d,]+)',page['text'])
            if not count:raise ValueError('Missing paginated source count')
            total,first,last=[int(v.replace(',','')) for v in count.groups()]
            if total!=len(entries) or first!=expected or last-first+1!=len(page['records']):raise ValueError('Captured results have a page gap or changing source count')
            expected=last+1;pages.append(page)
        if expected!=len(entries)+1 or [r for p in pages for r in p['records']]!=entries:raise ValueError('Captured pagination is incomplete or inconsistent')
    else:
        count=re.search(r'([\d,]+) items found, displaying all items',index['text'])
        if not count or int(count[1].replace(',',''))!=len(entries):raise ValueError('Captured index is incomplete')
    originals={'index':index,'pages':pages,'details':[]};records=[];identities=set()
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
    original=json.dumps(originals,ensure_ascii=False).encode()
    if len(original)>26214400:raise ValueError('Original source archive exceeds 25 MB; split the recording window')
    return records,original

def ingest(captures,district_id,start,end,*,commit=False):
    source=service.source_for(district_id);j=get_jurisdiction_by_district_id(district_id)
    if not j or j.id!=source['jurisdiction_id']:raise ValueError('District/source mismatch')
    from app.comptroller.cad_review import latest_completed_import
    cad_import=latest_completed_import(j.id) or {}
    records,original=prepare(captures,source,start,end)
    classifications=[]
    with ThreadPoolExecutor(max_workers=4) as pool:
        # Bound each name lookup so a year of filings cannot truncate the
        # indexed CRS/sales-tax context and hide later name-only candidates.
        for offset in range(0,len(records),50):
            batch=records[offset:offset+50]
            context=service.match_context(source,[{'assumed_name':r['assumed_name'],'owner_name':owner} for r in batch for owner in r['owners'] or [None]])
            def classify(r):return classify_detail(r,context,j)
            classifications.extend(pool.map(classify,batch))
    for r,intelligence in zip(records,classifications):r['intelligence']=intelligence
    if not commit:return {'records':len(records),'commit':False}
    return commit_prepared(source,j,records,original,start,end,result_pages=json.loads((captures/'index.json').read_text()).get('pages',1),cad_import_id=cad_import.get('id'))


def commit_prepared(source,j,records,original,start,end,*,result_pages=1,cad_import_id=None):
    from app.comptroller.cad_review import latest_completed_import
    latest=latest_completed_import(j.id) or {}
    if cad_import_id and latest.get('id')!=cad_import_id:raise ValueError('The BPP account export changed; match this set again before importing')
    district_id=source['district_id']
    identifier=str(uuid.uuid4());path=district_id+'/'+identifier+'.json'
    from .archive import build
    archive_data,archive_parts,archive_metadata=build(original,path)
    row=dict(id=identifier,source_id=source['id'],jurisdiction_id=j.id,district_id=district_id,filename='eagleweb-captured-records.json',sha256=hashlib.sha256(original).hexdigest(),storage_path=path,parser_version='eagleweb-captured-v2',status='PROCESSING',coverage='COMPLETE',page_count=0,reported_total=len(records),extracted_count=len(records),search_start=start,search_end=end,metadata={'storage_bucket':BUCKET,'collection_method':'bounded_browser_assisted','unattended':False,'result_pages':result_pages,'cad_import_id':cad_import_id or latest.get('id'),**archive_metadata})
    service.request('POST','county_clerk_dba_imports',payload=row)
    try:
        for part_path,part_data in archive_parts:
            service.storage('POST',part_path,part_data,bucket=BUCKET,content_type='application/json')
        service.storage('POST',path,archive_data,bucket=BUCKET,content_type='application/json')
        batches=[];batch=[]
        for record in records:
            if batch and (len(batch)>=25 or len(json.dumps(batch+[record]).encode())>60000):
                batches.append(batch);batch=[]
            batch.append(record)
        if batch:batches.append(batch)
        for number,batch in enumerate(batches):
            service.request('POST','rpc/dba_stage_import_batch',payload={'p_import_id':identifier,'p_batch_number':number,'p_filings':batch})
        return service.request('POST','rpc/dba_commit_staged_import',payload={'p_import_id':identifier})
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
