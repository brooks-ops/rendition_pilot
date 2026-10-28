"""Expose planned coverage separately from verified source collection."""
from . import service
from .schedule import schedule
from .connector import public_enabled


def for_source(source, *, now=None):
    plan = schedule(now)
    runs = service.request('GET', 'county_clerk_collection_runs', params={
        'select': 'id,scheduled_for,window_start,window_end,status,message,records_retrieved,imported_count,duplicate_count,created_at,completed_at,import_id,source_import:county_clerk_dba_imports(status,coverage,search_start,search_end,metadata,extracted_count)',
        'district_id': 'eq.' + source['district_id'], 'source_id': 'eq.' + source['id'],
        'order': 'scheduled_for.desc', 'limit': 8})
    october = plan['october_readiness_window']
    october_run = next((r for r in runs if r['scheduled_for'] == october['scheduled_for']), None)
    def fulfilled(run, window):
        imported=(run or {}).get('source_import') or {}
        return bool(run and run['status']=='COMPLETED' and imported.get('status')=='COMPLETED'
                    and imported.get('coverage')=='COMPLETE' and imported.get('search_start','9999')<=window['start']
                    and imported.get('search_end','0000')>=window['end']
                    and imported.get('extracted_count')==run.get('records_retrieved')
                    and run.get('records_retrieved') is not None)
    current_run=next((r for r in runs if r['scheduled_for']==plan['pending_window']['scheduled_for']),None)
    current_complete=fulfilled(current_run,plan['pending_window'])
    enabled=public_enabled(source)
    if current_complete:
        method=(current_run['source_import'].get('metadata') or {}).get('collection_method')
        method_label='Browser-assisted backfill' if method=='bounded_browser_assisted' else ('Automatic quarterly pull' if method=='public_guest_html' else 'Source import')
        message=(f"{method_label} complete: {plan['pending_window']['start']} through {plan['pending_window']['end']} · "
                 f"{current_run['records_retrieved']:,} filings matched against BPP accounts. "
                 + ('Quarterly automatic pulls are enabled. Next run: '+plan['next_run']['scheduled_for']+'.' if enabled else 'Quarterly unattended collection remains disabled.'))
    elif enabled:
        message=('Quarterly automatic DBA pulls are enabled: January 1, April 1, July 1 and October 1. '
                 'October 1 covers January–September for November/December work. '
                 'Current collection: '+str((current_run or {}).get('status','PENDING'))+'.')
    elif (current_run or {}).get('status')=='FAILED':
        message='Quarterly DBA collection stopped: '+str(current_run.get('message') or 'See collection history.')+' Existing filings and reviews are preserved.'
    else:
        message=('Quarterly DBA plan: January 1, April 1, July 1 and October 1. '
                 'October 1 covers January 1–September 30 for November/December work. '
                 'Automatic collection is blocked pending an approved source connection; '
                 'the full year-to-date filings have not been collected.')
    return {'configured': True, **plan, 'status': ('CURRENT' if current_complete else (current_run or {}).get('status','PENDING')) if enabled else ('BACKFILLED' if current_complete else ('FAILED' if (current_run or {}).get('status')=='FAILED' else 'BLOCKED')), 'automation_enabled': enabled,
            'last_successful_sync': source.get('last_successful_retrieval_at'),
            'last_run': runs[0] if runs else None, 'runs': runs,
            'october_collection_complete': fulfilled(october_run,october), 'message': message}


def for_district(district_id, *, now=None):
    try:
        source = service.source_for(district_id)
    except service.ImportError:
        return {'configured': False}
    return for_source(source, now=now)
