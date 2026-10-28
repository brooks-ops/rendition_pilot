"""Full-detail filing comparison using the existing district BPP matcher."""
from dataclasses import asdict
from copy import deepcopy
import re
from app.comptroller.matching import match_imported_bpp_accounts, normalize_name
from app.comptroller.property_adapter import get_property_adapter
from app.comptroller.property_matching import classify_account_type

def clean(s): return re.sub(r'\s+', ' ', s or '').strip() or None

def eligible(a,j):return a.get('account_status')!='NOT_IN_ACTIVE_DBA_EXPORT' and classify_account_type(a.get('real_account_number'),prefixes=j.account_type_prefixes)=='PERSONAL'
def name_candidates(record,context,j):
    names=[record['assumed_name'],*record['owners']];keys={normalize_name(n) for n in names};unique={}
    for a in sorted(context.get('accounts',[]),key=lambda x:x.get('tax_year') or 0,reverse=True):
        if not eligible(a,j):continue
        cad=[a.get('owner_name'),a.get('business_name'),a.get('dba_name')]
        if not keys.intersection(normalize_name(n) for n in cad if n):continue
        unique.setdefault(a['real_account_number'],{**a,'basis':'Exact normalized business/owner name; verify location','confidence':'MEDIUM'})
    return list(unique.values())

def compare(record,context,j,*,rejected_accounts=()):
    a=record['business_address'];street=clean(' '.join(filter(None,[(a or {}).get('line1'),(a or {}).get('line2')])))
    # Use the production CAD matcher unchanged. Each full owner is independent evidence.
    matches=[]
    if street:
        for owner in record['owners'] or [None]:
            result=match_imported_bpp_accounts(district_id=j.district_id,address=street,zip_code=a.get('zip'),legal_name=owner,business_name=record['assumed_name'])
            if result and (not result.candidate or result.candidate.account_number not in rejected_accounts):matches.append(asdict(result))
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
    return {'candidate_details':matched_details,'record':record,'status':status,'confidence':confidence,'matched_account':account,'reason':reason,'production_match':best,'per_owner_matches':matches,'name_only_candidates':by_name,'sales_tax_evidence':related_sales,'truncated_context':any(len(v)>1000 for v in context.values()),'notice':'Filing evidence only; no confirmed opening or closure.'}


def classify_detail(record, context, jurisdiction, *, rejected_accounts=()):
    record=deepcopy({k:v for k,v in record.items() if k!="intelligence"})
    result=compare(record,context,jurisdiction,rejected_accounts=rejected_accounts)
    best=result['production_match'] or {}
    candidate=best.get('candidate') if result['matched_account'] else None
    details=result.get('candidate_details') or {}
    classifications={'PROBABLE_EXISTING_ACCOUNT':'DBA_EXISTING_BUSINESS','NO_RELIABLE_MATCH':'DBA_POTENTIAL_NEW_BUSINESS','NEEDS_REVIEW':'DBA_NEEDS_REVIEW','ABANDONMENT_LEAD':'DBA_POTENTIAL_CLOSURE_LEAD'}
    candidates=list(result['name_only_candidates'])
    if details:
        candidates.insert(0,{**details,'id':details.get('property_id'),'basis':best.get('reason')})
    evidence={'filing':record,'account_candidates':candidates,'sales_tax_candidates':result['sales_tax_evidence'],'prior_filings':context.get('prior_filings',[]),'comparison':result,'notice':'Filing evidence only. No automatic taxable-account creation, closure or change.'}
    return dict(classification=classifications[result['status']],priority='MEDIUM' if candidate or result['status']=='ABANDONMENT_LEAD' else 'LOW',confidence=result['confidence'],confidence_score=best.get('score',0) if candidate else 0,is_ambiguous=result['status']=='NEEDS_REVIEW',matched_record_id=candidate['record_id'] if candidate else None,matched_account_number=result['matched_account'],matched_owner_name=details.get('owner_name') if candidate else None,matched_address=details.get('situs_address_raw') if candidate else None,match_reason=result['reason'],match_signals={'filing_address':'PROVIDED' if record.get('business_address') else 'NOT PROVIDED','owners':str(len(record.get('owners',[]))),'document':record['document_number']},recommended_action='Verify the source, review possible accounts, and record the outcome.',evidence=evidence)
