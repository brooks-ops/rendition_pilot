"""Server-attributed work events. Never accepts browser-supplied actors or district IDs."""
from __future__ import annotations

import csv
import hashlib
import io
import logging
from datetime import date
from app.comptroller.service import _request_json, get_supabase_config, postgrest_headers

ACTIONS = {
    'review_started', 'review_completed', 'review_dismissed', 'cad_match_decided',
    'account_summary_generated', 'rendition_locked', 'rendition_report_generated',
    'commercial_update_applied', 'commercial_update_undone', 'commercial_account_created',
    'commercial_report_generated', 'commercial_equity_generated',
}
MODULES = {'business_intelligence', 'mailing_changes', 'rendition', 'commercial'}
logger = logging.getLogger(__name__)


def record_activity(context, action, module, entity_type, entity_id, *, outcome=None, units=1, dedup_key=None):
    """Record only after a successful operation. Telemetry outages don't undo saved work.

    No notes, documents, addresses, values, credentials or client metadata are stored.
    Stable operation IDs deduplicate retries where the workflow provides one.
    """
    try:
        if action not in ACTIONS or module not in MODULES:
            raise ValueError('Unknown activity type')
        if not context.user_id or not context.district_id or not entity_id:
            return
        payload = dict(district_id=context.district_id, actor_id=context.user_id,
                       actor_email=context.email, action=action, module=module,
                       entity_type=str(entity_type)[:80], entity_id=str(entity_id)[:200],
                       outcome=str(outcome)[:80] if outcome else None, units=max(1, int(units)))
        if dedup_key:
            payload['dedup_key'] = hashlib.sha256(f'{context.district_id}|{context.user_id}|{action}|{dedup_key}'.encode()).hexdigest()
        url, key = get_supabase_config()
        _request_json('POST', f'{url}/rest/v1/appraiser_activity_events',
                      postgrest_headers(key, prefer='resolution=ignore-duplicates,return=minimal'),
                      params={'on_conflict': 'dedup_key'}, json_payload=payload)
    except Exception:
        # Do not log request bodies or provider errors, which may contain private data.
        logger.error('Appraiser activity event could not be recorded (action=%s).', action)


def report(district_id, start, end, workspace='all'):
    if workspace not in ('all', 'bpp', 'commercial'):
        raise ValueError('Unknown workspace')
    start_date, end_date = date.fromisoformat(start), date.fromisoformat(end)
    if end_date < start_date or (end_date-start_date).days > 365:
        raise ValueError('Choose a date range of up to 366 days.')
    url, key = get_supabase_config()
    return _request_json('POST', f'{url}/rest/v1/rpc/appraiser_activity_report_v2', postgrest_headers(key),
                         json_payload={'p_district_id': district_id, 'p_start': start, 'p_end': end, 'p_workspace': workspace})


def csv_report(data, *, sales=False):
    stream = io.StringIO(newline='')
    writer = csv.writer(stream)
    def safe(value):
        text = str(value if value is not None else '')
        return "'"+text if text.startswith(('=', '+', '-', '@', '\t', '\r', '\n')) else text
    if sales:
        writer.writerow(['Workspace', 'Period start', 'Period end', 'Tracking started', 'Metric', 'Count'])
        for key, value in data['totals'].items():
            writer.writerow([data.get('workspace', 'all'), data['start'], data['end'], data.get('tracking_started_at') or '', key, value])
        for row in data.get('outcomes', []):
            metric = ' / '.join(str(row.get(k) or '') for k in ('module', 'action', 'outcome'))
            writer.writerow([data.get('workspace', 'all'), data['start'], data['end'], data.get('tracking_started_at') or '', safe(metric)+' / distinct work items', row.get('work_items', 0)])
        writer.writerow(['', '', '', '', 'Counts are recorded product activity, not verified CAD changes or measured time savings.', ''])
    else:
        columns = ['appraiser', 'actions', 'work_items', 'completed', 'started', 'dismissed', 'renditions', 'cad_decisions', 'commercial_updates', 'commercial_rows_updated', 'commercial_accounts_created', 'commercial_undos', 'commercial_reports', 'commercial_report_rows', 'commercial_equity_analyses', 'reports', 'active_days']
        if data.get('workspace') == 'commercial':
            columns = ['appraiser', 'commercial_updates', 'commercial_rows_updated', 'commercial_accounts_created', 'commercial_undos', 'commercial_reports', 'commercial_report_rows', 'commercial_equity_analyses', 'active_days', 'actions']
        writer.writerow(columns)
        for row in data['appraisers']:
            writer.writerow([safe(row.get(k, 0)) for k in columns])
    return stream.getvalue()
