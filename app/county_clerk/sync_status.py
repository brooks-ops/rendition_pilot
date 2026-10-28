"""Operational visibility while a supported source connection awaits approval.

No source requests, scheduled jobs, or success timestamps are fabricated here.
"""
from concurrent.futures import ThreadPoolExecutor
from . import service
from .schedule import schedule
from .collection_status import for_source

ACTIVATION_REQUIREMENTS=[
    'County or licensed provider approves unattended retrieval and Rendition Pilot use.',
    'Supported endpoint or scheduled export, field specification, credentials and rate limits are supplied.',
    'A complete unattended collection is reconciled against source counts, including abandonments and delayed records.'
]

def count_rows(table,district_id,**filters):
    url,key=service.get_supabase_config()
    response=service.requests.get(url+'/rest/v1/'+table,
        headers=service.postgrest_headers(key,prefer='count=exact'),
        params={'select':'id','district_id':'eq.'+district_id,'limit':1,**filters},timeout=25)
    response.raise_for_status()
    total=response.headers.get('Content-Range','').rsplit('/',1)[-1]
    if not total.isdigit():raise service.ComptrollerServiceError('County filing totals are unavailable.')
    return int(total)

def dashboard(district_id):
    try:history=service.history(district_id)
    except service.ImportError as exc:return {'configured':False,'message':str(exc),'automation_enabled':False,'manual_sync_available':False}
    source=history['source']
    plan=schedule()
    collection=for_source(source)
    specs={
        'new_filings':('county_clerk_dba_filings',{'filing_type':'eq.ASSUMED NAME'}),
        'abandonments':('county_clerk_dba_filings',{'filing_type':'eq.ASSUMED NAME ABANDONMENT'}),
        'matched_to_crs':('bpp_intelligence_items',{'signal_type':'eq.county_clerk_dba','matched_account_number':'not.is.null'}),
        'requiring_investigation':('bpp_intelligence_items',{'signal_type':'eq.county_clerk_dba','status':'in.(NEW,IN_REVIEW)','or':'(classification.eq.DBA_NEEDS_REVIEW,is_ambiguous.eq.true,matched_account_number.is.null)'}),
        'missing_full_owner':('county_clerk_dba_filings',{'owner_name_complete':'eq.false'}),
        'missing_business_address':('county_clerk_dba_filings',{'original_fields->>business_address':'is.null'}),
    }
    def count(spec):return count_rows(spec[0],district_id,**spec[1])
    with ThreadPoolExecutor(max_workers=3) as pool:metrics=dict(zip(specs,pool.map(count,specs.values())))
    return {
        'configured':True,'county':source['county'],'state':source['state'],
        'connection_status':'AWAITING_APPROVED_CONNECTION',**plan,
        'automation_enabled':False,'manual_sync_available':False,
        'last_successful_sync':source.get('last_successful_retrieval_at'),
        'last_successful_fallback_import':source.get('last_successful_import_at'),
        'metrics_scope':'All imported county filings, including PDF fallback imports. These are not quarterly automated-sync counts.',
        'metrics':metrics,
        'sync_message':collection['message'],'coverage_status':collection['status'],'october_collection_complete':collection['october_collection_complete'],
        'activation_requirements':ACTIVATION_REQUIREMENTS,
        'sync_errors':[],
        'sync_error_note':'No direct collection runs have occurred; no successful unattended run has been demonstrated.',
        'fallback_import_issues':[{k:r.get(k) for k in ('id','filename','status','created_at','error_message')} for r in history['imports'] if r['status'] in ('FAILED','PROCESSING','PARTIAL')],
        'next_window':plan['pending_window'],
    }
