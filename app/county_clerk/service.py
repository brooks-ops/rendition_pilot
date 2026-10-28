"""District-scoped filing imports into the existing BPP intelligence queue."""
from copy import deepcopy
from datetime import datetime,timezone
from pathlib import Path
import re,uuid
import requests
from app.comptroller.service import _request_json,get_supabase_config,postgrest_headers,ComptrollerServiceError
from app.comptroller.jurisdictions import get_jurisdiction_by_district_id
from app.comptroller.property_matching import classify_account_type
from app.comptroller.matching import _name_similarity
from .pdf_import import parse_pdf,ImportError
from .connector import EagleWebConnector,next_window

BUCKET='county-clerk-dba'
def request(method,table,*,params=None,payload=None):
    url,key=get_supabase_config()
    return _request_json(method,url+'/rest/v1/'+table,postgrest_headers(key,prefer='return=representation'),params=params,json_payload=payload)
def source_for(district_id):
    rows=request('GET','county_clerk_sources',params={'select':'*','district_id':'eq.'+district_id,'manual_import_enabled':'eq.true','limit':2})
    if len(rows)!=1:raise ImportError('County Clerk PDF imports are not configured for this district. Ask the owner to configure its county source.')
    return rows[0]
def storage(method,path,data=None,*,bucket=BUCKET,content_type="application/pdf"):
    url,key=get_supabase_config()
    r=requests.request(method,url+'/storage/v1/object/'+bucket+'/'+path,headers={'apikey':key,'Authorization':'Bearer '+key,'Content-Type':content_type},data=data,timeout=90)
    r.raise_for_status();return r

def name_key(name):return re.sub('[^a-z0-9]','',(name or '').lower())
def match_context(source,records):
    names=sorted({n for r in records for n in [r['assumed_name'],*(r.get('owners') or [r.get('owner_name')])] if n and len(name_key(n))>=3})
    return request('POST','rpc/dba_match_context',payload={'p_district_id':source['district_id'],'p_jurisdiction_id':source['jurisdiction_id'],'p_names':names})

def classify(record,context,jurisdiction,*,decision=None):
    """Scores evidence, not whether a business has actually opened/closed."""
    names=[record['assumed_name']]+([record['owner_name']] if record.get('owner_name_complete') and record.get('owner_name') else [])
    keys={name_key(n) for n in names}
    candidates=[];truncated=any(len(context.get(k,[]))>1000 for k in ('accounts','sales_tax','prior_filings'))
    for a in context.get('accounts',[])[:1000]:
        if a.get('account_status')=='NOT_IN_ACTIVE_DBA_EXPORT' or classify_account_type(a.get('real_account_number'),prefixes=jurisdiction.account_type_prefixes)!='PERSONAL':continue
        cad_names=[a.get('owner_name'),a.get('business_name'),a.get('dba_name')]
        score=max((_name_similarity(n,c) for n in names for c in cad_names),default=0)
        if not keys.intersection(name_key(n) for n in cad_names if n) and not (decision and a.get('id')==decision.get('property_record_id')):continue
        candidates.append({**a,'score':round(score,3),'basis':'Exact normalized name; filing address is unavailable'})
    # Multiple annual rows of one account are not multiple businesses.
    unique={}
    for c in sorted(candidates,key=lambda a:((a.get('tax_year') or 0),a['id']),reverse=True):unique.setdefault(c['real_account_number'],c)
    candidates=list(unique.values());candidates.sort(key=lambda a:-a['score'])
    sales=[r for r in context.get('sales_tax',[])[:1000] if keys.intersection({name_key(r.get('legal_name')),name_key(r.get('location_name'))})]
    prior=[r for r in context.get('prior_filings',[])[:1000] if name_key(r['assumed_name']) in keys and not (r['document_number']==record['document_number'] and r['filing_type']==record['filing_type'])]
    best=None;confidence='UNMATCHED';score=0;ambiguous=len(candidates)>1 or truncated
    classification='DBA_POTENTIAL_NEW_BUSINESS';reason='No reliable existing BPP account match was found in the loaded data. A filing alone does not prove the business opened.'
    if candidates:
        classification='DBA_EXISTING_BUSINESS';reason='An existing BPP account has a matching name. This is a probable match; verify the individual filing and business location.';confidence='MEDIUM';score=.75
        if len(candidates)==1:best=candidates[0]
    if prior and any(r['filing_type']!=record['filing_type'] or (r.get('owner_name') and record.get('owner_name') and name_key(r['owner_name'])!=name_key(record['owner_name'])) for r in prior):
        classification='DBA_POSSIBLE_BUSINESS_CHANGE';reason='Earlier filings share this name but differ in filing type or available owner information. Investigate; no taxable account was changed.'
    if ambiguous:classification='DBA_NEEDS_REVIEW';confidence='LOW';score=.4;best=None;reason='Multiple possible accounts or truncated candidate evidence require appraiser review.'
    if not record.get('owner_name_complete') and not candidates:
        classification='DBA_NEEDS_REVIEW';confidence='LOW';score=.25;reason='The results PDF does not provide a complete owner name or business address. Retrieve the individual filing before deciding whether this is a new business.'
    if record['filing_type']=='ASSUMED NAME ABANDONMENT':
        classification='DBA_POTENTIAL_CLOSURE_LEAD';reason='Assumed-name abandonment is a closure lead only. It does not prove that this business or taxable account closed.'
    if decision:
        best=next((c for c in candidates if c['id']==decision['property_record_id']),None)
        if best:
            if classification=='DBA_NEEDS_REVIEW':classification='DBA_EXISTING_BUSINESS'
            confidence='HIGH';score=1.0;ambiguous=False;reason='Appraiser confirmed the linked BPP account. '+(decision.get('notes') or '')
    evidence={'filing':{k:v for k,v in record.items() if k!='intelligence'},'account_candidates':candidates[:20],'sales_tax_candidates':sales[:20],'prior_filings':prior[:20],'truncated':truncated,'owner_text_used_for_matching':bool(record.get('owner_name_complete')),'human_decision':decision,'notice':'Filing evidence only. No automatic taxable-account creation, closure or change.'}
    return {'classification':classification,'priority':'MEDIUM' if best or record['filing_type']=='ASSUMED NAME ABANDONMENT' else 'LOW','confidence':confidence,'confidence_score':score,'is_ambiguous':ambiguous,'matched_record_id':best['id'] if best else None,'matched_account_number':best['real_account_number'] if best else None,'matched_owner_name':best.get('owner_name') if best else None,'matched_address':best.get('situs_address_raw') if best else None,'match_reason':reason,'match_signals':{'name_evidence':'EXACT NORMALIZED NAME' if candidates else 'NO RELIABLE MATCH','filing_address':'NOT PROVIDED','owner':'COMPLETE SOURCE NAME' if record.get('owner_name_complete') else 'MISSING / CLIPPED — NOT USED','sales_tax_candidates':str(len(sales)),'prior_filings':str(len(prior))},'recommended_action':'Open the individual filing, verify the account and location, then record the review outcome.','evidence':evidence}

def preview(district_id,data):
    source=source_for(district_id);parsed=parse_pdf(data,source_url=source['source_url'])
    connector=EagleWebConnector(source)
    for r in parsed['records']:r['document_reference']=connector.record_reference(r['document_reference'])
    parsed['county']=source['county'];parsed['state']=source['state']
    return parsed

def commit(context,data,filename,expected_sha256,acknowledge_incomplete):
    source=source_for(context.district_id);parsed=preview(context.district_id,data)
    if parsed['sha256']!=expected_sha256:raise ImportError('The file changed. Preview it again before importing.')
    if not parsed['records']:raise ImportError('No usable filings were found; nothing was imported.')
    if len(parsed['records'])>3000:raise ImportError('Split this export into PDFs with at most 3,000 filings.')
    if parsed['coverage']!='COMPLETE' and not acknowledge_incomplete:raise ImportError('Acknowledge the incomplete or unverified result set before importing only these filings.')
    identifier=str(uuid.uuid4());path=context.district_id+'/'+identifier+'.pdf'
    row={'id':identifier,'source_id':source['id'],'jurisdiction_id':source['jurisdiction_id'],'district_id':context.district_id,'filename':Path(filename).name[:255],'sha256':parsed['sha256'],'storage_path':path,'parser_version':parsed['parser_version'],'status':'PROCESSING','coverage':parsed['coverage'],'page_count':parsed['page_count'],'reported_total':parsed['reported_total'],'extracted_count':parsed['record_count'],'search_start':parsed['search_start'],'search_end':parsed['search_end'],'metadata':{k:parsed[k] for k in ('warnings','search_windows','rejected_records','notice')},'created_by':context.user_id}
    request('POST','county_clerk_dba_imports',payload=row)
    try:
        storage('POST',path,data)
        jurisdiction=get_jurisdiction_by_district_id(context.district_id)
        if not jurisdiction or jurisdiction.id!=source['jurisdiction_id']:raise ImportError('The county source and district jurisdiction do not match.')
        evidence=match_context(source,parsed['records'])
        records=deepcopy(parsed['records'])
        for record in records:record['intelligence']=classify(record,evidence,jurisdiction)
        return request('POST','rpc/dba_commit_import',payload={'p_import_id':identifier,'p_filings':records})
    except Exception:
        # A response timeout can occur after the atomic RPC committed. Never overwrite success.
        current=request('GET','county_clerk_dba_imports',params={'select':'id,status,imported_count,duplicate_count','id':'eq.'+identifier,'district_id':'eq.'+context.district_id,'limit':1})
        if current and current[0]['status'] in ('COMPLETED','PARTIAL'):
            return {**current[0],'import_id':identifier}
        request('PATCH','county_clerk_dba_imports',params={'id':'eq.'+identifier,'district_id':'eq.'+context.district_id,'status':'eq.PROCESSING'},payload={'status':'FAILED','error_message':'Import did not finish. Retry this PDF; filing deduplication protects existing work.','completed_at':datetime.now(timezone.utc).isoformat()})
        raise

def history(district_id):
    source=source_for(district_id)
    rows=request('GET','county_clerk_dba_imports',params={'select':'id,filename,status,coverage,page_count,extracted_count,imported_count,duplicate_count,search_start,search_end,created_at,completed_at,error_message,metadata','district_id':'eq.'+district_id,'order':'created_at.desc','limit':50})
    from .schedule import schedule
    return {'source':source,'imports':rows,'next_window':schedule()['pending_window']}

def original(district_id,import_id):
    rows=request('GET','county_clerk_dba_imports',params={'select':'storage_path,filename,metadata','district_id':'eq.'+district_id,'id':'eq.'+import_id,'limit':1})
    if not rows:raise ImportError('This PDF import was not found in your district.')
    return storage('GET',rows[0]['storage_path'],bucket=rows[0].get('metadata',{}).get('storage_bucket',BUCKET)).content,rows[0]['filename']

def refresh_item(item):
    filing_id=item.raw.get('source_dba_filing_id');rows=request('GET','county_clerk_dba_filings',params={'select':'*','district_id':'eq.'+item.district_id,'id':'eq.'+filing_id,'limit':1})
    if not rows:raise ImportError('The source filing was not found.')
    record=rows[0]['original_fields'];source=source_for(item.district_id);j=get_jurisdiction_by_district_id(item.district_id)
    ctx=match_context(source,[record])
    from app.comptroller.cad_review import decision_history,current_decisions
    decision,decisions=current_decisions(decision_history(j.id,None,filing_id=filing_id))
    from app.comptroller.cad_review import normalize_account_number
    rejected={a for a,d in decisions.items() if d["decision"]=="REJECTED"}
    ctx["accounts"]=[a for a in ctx["accounts"] if normalize_account_number(a["real_account_number"]) not in rejected]
    if decision and not any(a['id']==decision['property_record_id'] for a in ctx['accounts']):
        from app.comptroller.property_adapter import get_property_adapter
        from dataclasses import asdict
        p=get_property_adapter(j).get_property_by_id(j,decision['property_record_id'])
        if p:ctx['accounts'].append({**asdict(p),'id':p.property_id})
    if record.get('owners') and not decision:
        from .detailed import classify_detail
        updated=classify_detail(record,ctx,j,rejected_accounts=rejected)
    else:
        updated=classify(record,ctx,j,decision=decision)
    request('PATCH','bpp_intelligence_items',params={'id':'eq.'+item.id,'district_id':'eq.'+item.district_id},payload=updated)
