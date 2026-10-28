"""Expose planned coverage separately from verified source collection."""
from . import service
from .schedule import schedule


def for_source(source, *, now=None):
    plan = schedule(now)
    runs = service.request('GET', 'county_clerk_collection_runs', params={
        'select': 'id,scheduled_for,window_start,window_end,status,message,records_retrieved,imported_count,duplicate_count,created_at,completed_at',
        'district_id': 'eq.' + source['district_id'], 'source_id': 'eq.' + source['id'],
        'order': 'scheduled_for.desc', 'limit': 8})
    october = plan['october_readiness_window']
    october_run = next((r for r in runs if r['scheduled_for'] == october['scheduled_for']), None)
    return {'configured': True, **plan, 'status': 'BLOCKED', 'automation_enabled': False,
            'last_successful_sync': source.get('last_successful_retrieval_at'),
            'last_run': runs[0] if runs else None, 'runs': runs,
            'october_collection_complete': bool(october_run and october_run['status'] == 'COMPLETED'),
            'message': 'Quarterly DBA plan: January 1, April 1, July 1 and October 1. '
                       'October 1 covers January 1–September 30 for November/December work. '
                       'Automatic collection is blocked pending an approved source connection; '
                       'the full year-to-date filings have not been collected.'}


def for_district(district_id, *, now=None):
    try:
        source = service.source_for(district_id)
    except service.ImportError:
        return {'configured': False}
    return for_source(source, now=now)
