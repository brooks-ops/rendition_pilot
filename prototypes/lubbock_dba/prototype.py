"""Parse browser-captured public details and compare to existing district CAD.

No EagleWeb networking, database mutations, production routes or scheduling.
"""
from __future__ import annotations
import argparse,csv,hashlib,html,json,re
from collections import Counter
from dataclasses import asdict
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from app.comptroller.matching import match_imported_bpp_accounts,normalize_name
from app.comptroller.jurisdictions import get_jurisdiction_by_district_id
from app.comptroller.property_adapter import get_property_adapter
from app.comptroller.property_matching import classify_account_type
from app.county_clerk.service import match_context

HOST='erecord.lubbockcounty.gov'
def clean(s):return re.sub(r'\s+',' ',s or '').strip() or None
def field_value(group,label):
    if group is None:return None
    el=next((x for x in group.select('span.fieldLabel') if clean(x.get_text())==label),None)
    return el.find_next_sibling('span',class_='field') if el else None
def value(group,label):
    el=field_value(group,label);return clean(el.get_text(' ',strip=True)) if el else None
def address(group,label):
    block=field_value(group,label)
    if block is None:return None
    fields={k:value(block,v) for k,v in [('line1','Address1'),('line2','Address2'),('city','City'),('state','State'),('zip','Zip')]}
    return fields if any(fields.values()) else None

def parse_detail(capture,index_record):
    url=urlparse(capture.get('url',''))
    if url.scheme!='https' or url.hostname!=HOST or url.path!='/recorder/eagleweb/viewDoc.jsp':raise ValueError('Unrecognized source record URL')
    kind,number=index_record['description'].strip().rsplit('\n',1)
    if kind not in ('ASSUMED NAME','ASSUMED NAME ABANDONMENT') or not re.fullmatch(r'\d{10}',number):raise ValueError('Unrecognized filing identity')
    root=BeautifulSoup(capture.get('html') or '','html.parser').find(id='presentation')
    if root is None or value(root,'Document Number')!=number:raise ValueError('Detail/summary identity mismatch')
    if not re.search(re.escape(kind)+r'\s*-\s*'+re.escape(number),capture.get('body','')):raise ValueError('Detail filing type does not match index')
    groups={clean(f.legend.get_text()):f for f in root.find_all('fieldset') if f.legend}
    business=groups.get('AssumedName');owner_group=groups.get('Owner(s)');owners=[]
    if owner_group:
        for table in owner_group.find_all('table'):
            if table.find('table'):continue
            rows=table.find_all('tr');headers=[clean(c.get_text()) for c in rows[0].find_all(['td','th'],recursive=False)] if rows else []
            name_index=next((i for i,h in enumerate(headers) if h in ('Name(s)','Name','Owner Name')),None)
            if name_index is None:continue
            for row in rows[1:]:
                cells=row.find_all(['td','th'],recursive=False)
                if len(cells)>name_index:
                    owner=clean(cells[name_index].get_text(' ',strip=True))
                    if owner and owner not in owners:owners.append(owner)
    recorded_raw=value(root,'Filing Date')
    try:recorded=datetime.strptime(recorded_raw,'%m/%d/%Y %I:%M:%S %p')
    except (TypeError,ValueError) as exc:raise ValueError('Missing or invalid filing timestamp') from exc
    expiry_raw=value(root,'Expiration Date')
    expiry=datetime.strptime(expiry_raw,'%m/%d/%Y').date().isoformat() if expiry_raw else None
    business_address=address(business,'Business Address');mailing_address=address(business,'Mailing Address') or address(root,'Mailing Address')
    assumed=value(business,'Name')
    if not assumed:raise ValueError('Assumed name missing')
    return {'state':'TX','county':'LUBBOCK','document_number':number,'filing_type':kind,'recording_date':recorded.date().isoformat(),'recording_timestamp_local':recorded.isoformat(),'recording_timezone':'America/Chicago','recording_date_raw':recorded_raw,'assumed_name':assumed,'owners':owners,'owner_name':owners[0] if len(owners)==1 else None,'owner_name_complete':bool(owners),'business_address':business_address,'mailing_address':mailing_address,'business_conducted_as':value(business,'Business Conducted As'),'expiration_date':expiry,'additional_groups':{k:clean(v.get_text(' ',strip=True)) for k,v in groups.items() if k not in ('AssumedName','Owner(s)')},'source_url':capture['url'],'source_sha256':hashlib.sha256(capture['html'].encode()).hexdigest(),'missing_fields':[k for k,v in [('owners',owners),('business_address',business_address),('mailing_address',mailing_address),('expiration_date',expiry)] if not v],'images_available_online':False if "Images are available at the County Clerk's office" in capture.get('body','') else None}

def eligible(a,j):return a.get('account_status')!='NOT_IN_ACTIVE_DBA_EXPORT' and classify_account_type(a.get('real_account_number'),prefixes=j.account_type_prefixes)=='PERSONAL'
def name_candidates(record,context,j):
    names=[record['assumed_name'],*record['owners']];keys={normalize_name(n) for n in names};unique={}
    for a in sorted(context.get('accounts',[]),key=lambda x:x.get('tax_year') or 0,reverse=True):
        if not eligible(a,j):continue
        cad=[a.get('owner_name'),a.get('business_name'),a.get('dba_name')]
        if not keys.intersection(normalize_name(n) for n in cad if n):continue
        unique.setdefault(a['real_account_number'],{**a,'basis':'Exact normalized business/owner name; verify location','confidence':'MEDIUM'})
    return list(unique.values())

def compare(record,context,j):
    a=record['business_address'];street=clean(' '.join(filter(None,[(a or {}).get('line1'),(a or {}).get('line2')])))
    # Use the production CAD matcher unchanged. Each full owner is independent evidence.
    matches=[]
    if street:
        for owner in record['owners'] or [None]:
            result=match_imported_bpp_accounts(district_id=j.district_id,address=street,zip_code=a.get('zip'),legal_name=owner,business_name=record['assumed_name'])
            if result:matches.append(asdict(result))
    rank={'HIGH':3,'MEDIUM':2,'LOW':1,'UNMATCHED':0}
    matches.sort(key=lambda m:(rank[m['confidence']],m['score']),reverse=True)
    best=matches[0] if matches else None
    strong_accounts={m['candidate']['account_number'] for m in matches if m.get('candidate') and m['confidence'] in ('HIGH','MEDIUM')}
    by_name=name_candidates(record,context,j)
    related_sales=[s for s in context.get('sales_tax',[]) if any(normalize_name(n) in {normalize_name(s.get('location_name')),normalize_name(s.get('legal_name'))} for n in [record['assumed_name'],*record['owners']])]
    status='NO_RELIABLE_MATCH';account=None;confidence='UNMATCHED';reason='No reliable match in currently loaded BPP account data. This does not prove the business is new.'
    if len(strong_accounts)>1:
        status='NEEDS_REVIEW';confidence='LOW';reason='Different full owners point to different BPP accounts; do not choose automatically.'
    elif best and best['confidence']=='HIGH' and not best['ambiguous'] and best['candidate']:
        status='PROBABLE_EXISTING_ACCOUNT';confidence=best['confidence'];account=best['candidate']['account_number'];reason=best['reason']
    elif best:
        status='NEEDS_REVIEW';confidence=best['confidence'];reason=best['reason']
    if not account and by_name:
        status='NEEDS_REVIEW';confidence='MEDIUM' if len(by_name)==1 else 'LOW';reason='Exact name evidence exists, but location or identity still needs verification.'
        # Keep name-only suggestions separate when situs data conflicts or is absent.
    if record['filing_type']=='ASSUMED NAME ABANDONMENT':status='ABANDONMENT_LEAD';reason='Abandonment is a lead, not proof of closure. '+reason
    matched_details=None
    if best and best.get('candidate'):
        prop=get_property_adapter(j).get_property_by_id(j,best['candidate']['record_id'])
        matched_details=asdict(prop) if prop else None
    return {'candidate_details':matched_details,'record':record,'status':status,'confidence':confidence,'matched_account':account,'reason':reason,'production_match':best,'per_owner_matches':matches,'name_only_candidates':by_name,'sales_tax_evidence':related_sales,'truncated_context':any(len(v)>1000 for v in context.values()),'notice':'Read-only prototype. No account/queue changes; no confirmed opening or closure.'}

def run(captures,district_id,output):
    j=get_jurisdiction_by_district_id(district_id)
    if not j or j.county_name.upper()!='LUBBOCK' or j.state!='TX':raise ValueError('This bounded prototype is for the configured Lubbock district only')
    index=json.loads((captures/'index.json').read_text());records=[];missing=[]
    total_match=re.search(r'([\d,]+) items found, displaying all items',index['text'])
    reported_total=int(total_match[1].replace(',','')) if total_match else None
    if len(index['records'])>30:raise ValueError('Prototype limit: at most 30 captured source records')
    if len({r['description'].strip() for r in index['records']})!=len(index['records']):raise ValueError('Duplicate index identities')
    for entry in index['records']:
        number=entry['description'].strip().rsplit('\n',1)[1];path=captures/(number+'.json')
        if not path.exists():missing.append(number);continue
        records.append(parse_detail(json.loads(path.read_text()),entry))
    # Reuse the existing indexed district-only name context RPC (read-only).
    query_records=[{'assumed_name':r['assumed_name'],'owner_name':owner} for r in records for owner in r['owners'] or [None]]
    ctx=match_context({'district_id':j.district_id,'jurisdiction_id':j.id},query_records)
    results=[]
    for i,r in enumerate(records,1):
        results.append(compare(r,ctx,j));print(f"Matched source filing {i}/{len(records)}",flush=True)
    for result in results:
        result['related_filings_in_sample']=[{k:other['record'][k] for k in ('document_number','filing_type','recording_date','owners')} for other in results if other is not result and normalize_name(other['record']['assumed_name'])==normalize_name(result['record']['assumed_name'])]
    from app.comptroller.cad_review import latest_completed_import
    latest=latest_completed_import(j.id) or {}
    export={k:latest.get(k) for k in ('id','source_as_of_date','imported_at','import_status','row_count')}
    summary={'cad_export':export,'reported_total':reported_total,'index_complete':reported_total==len(index['records']),'recording_min':min((r['recording_date'] for r in records),default=None),'recording_max':max((r['recording_date'] for r in records),default=None),'district_id':district_id,'jurisdiction_id':j.id,'collected_at':datetime.now(timezone.utc).isoformat(),'search_start':'2026-10-19','search_end_requested':'2026-10-28','search_evidence':index['text'].split('You searched for:')[-1].split('Description')[0].strip(),'index_records':len(index['records']),'details_retrieved':len(records),'missing_documents':missing,'filing_types':dict(Counter(r['filing_type'] for r in records)),'with_full_owners':sum(bool(r['owners']) for r in records),'with_business_address':sum(bool(r['business_address']) for r in records),'with_mailing_address':sum(bool(r['mailing_address']) for r in records),'outcomes':dict(Counter(r['status'] for r in results)),'confidence':dict(Counter(r['confidence'] for r in results)),'read_only':True,'unattended_collection_enabled':False}
    output.mkdir(parents=True,exist_ok=True)
    (output/'results.json').write_text(json.dumps({'summary':summary,'results':results},indent=2))
    with (output/'filings.csv').open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=['document_number','filing_type','recording_date','assumed_name','owners','business_address','mailing_address','expiration_date','status','confidence','matched_account','suggested_accounts','reason','source_url']);writer.writeheader()
        for r in results:
            rec=r['record'];writer.writerow({**{k:rec.get(k) for k in ['document_number','filing_type','recording_date','assumed_name','expiration_date','source_url']},'owners':' | '.join(rec['owners']),'business_address':json.dumps(rec['business_address']) if rec['business_address'] else '', 'mailing_address':json.dumps(rec['mailing_address']) if rec['mailing_address'] else '',**{k:r[k] for k in ['status','confidence','matched_account','reason']},'suggested_accounts':' | '.join(sorted({c['real_account_number'] for c in r['name_only_candidates']} | set(filter(None,((r.get('production_match') or {}).get('signals') or {}).get('personal_property_accounts','').split(', ')))))})
    print(json.dumps(summary,indent=2));return summary

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--captures',type=Path,required=True);p.add_argument('--district-id',required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--env-file',type=Path);args=p.parse_args();
    if args.env_file:
        from dotenv import load_dotenv
        load_dotenv(args.env_file,override=True)
    run(args.captures,args.district_id,args.output)
