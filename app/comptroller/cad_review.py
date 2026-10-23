"""CAD candidate review, durable human decisions, and import/match health.

All writes are advisory records inside Rendition Pilot. Decisions are an
append-only history keyed to a permit and CAD account, independent of an
alert's workflow status and of automatic matching/re-imports.
"""
from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo
from dataclasses import asdict
from datetime import date, datetime, timezone
from typing import Any

import requests

from app.comptroller.address_normalizer import normalize_address
from app.comptroller.property_adapter import get_property_adapter, eligible_for_current_dba_match
from app.comptroller.property_matching import classify_account_type, score_property_candidate, normalize_account_number
from app.comptroller.service import ComptrollerServiceError, _paginated_get, _request_json, get_supabase_config, postgrest_headers


class CadReviewError(ValueError):
    pass


def _request(method, table, *, params=None, payload=None):
    url, key = get_supabase_config()
    return _request_json(method, f'{url}/rest/v1/{table}', postgrest_headers(key, prefer='return=representation'), params=params, json_payload=payload)


def decision_history(jurisdiction_id: str, permit_location_id: str) -> list[dict]:
    url, key = get_supabase_config()
    return _paginated_get(url, postgrest_headers(key), 'cad_match_decisions', {
        'select': '*', 'jurisdiction_id': f'eq.{jurisdiction_id}',
        'permit_location_id': f'eq.{permit_location_id}', 'order': 'reviewed_at.desc,id.desc',
    }, page_size=500)


def current_decisions(history: list[dict]) -> tuple[dict | None, dict[str, dict]]:
    """Newest confirmation wins; rejecting it never revives an older one."""
    by_account = {}
    latest_confirmation = None
    for row in sorted(history, key=lambda r: (r['reviewed_at'], r['id']), reverse=True):
        by_account.setdefault(normalize_account_number(row['account_number']), row)
        if latest_confirmation is None and row['decision'] == 'CONFIRMED':
            latest_confirmation = row
    confirmed = latest_confirmation
    if confirmed and by_account[normalize_account_number(confirmed['account_number'])]['decision'] != 'CONFIRMED':
        confirmed = None
    return confirmed, by_account


def item_permit_id(item) -> str | None:
    return item.raw.get('source_permit_location_id') or item.raw.get('permit_location_id')


def candidate_review(jurisdiction, item, *, account_number: str | None = None, offset: int = 0) -> dict:
    from app.comptroller.matching import _name_similarity
    adapter = get_property_adapter(jurisdiction)
    normalized = normalize_address(item.source_address, zip_code=item.source_zip)
    if account_number:
        records = adapter.find_properties_by_real_account(jurisdiction, account_number.strip().upper())
    else:
        records = adapter.find_properties_by_address(jurisdiction, normalized.base_address, limit=500) if normalized.base_address else []
    permit_id = item_permit_id(item)
    history = decision_history(jurisdiction.id, permit_id) if permit_id else []
    confirmed, decisions = current_decisions(history)
    candidates = []
    for prop in records:
        if not eligible_for_current_dba_match(prop):
            continue
        if classify_account_type(prop.real_account_number, prefixes=jurisdiction.account_type_prefixes) != 'PERSONAL':
            continue
        score = score_property_candidate(normalized, prop)
        cad_names = [prop.owner_name, prop.business_name, prop.dba_name]
        name_score = max((_name_similarity(a, b) for a in [item.legal_name, item.business_name] for b in cad_names), default=0)
        conflict = score.suite_conflict or score.zip_signal == 'NO MATCH' or not score.street_number_match or score.street_name_signal not in ('MATCH','NORMALIZED MATCH')
        row = asdict(prop)
        row.update({'address_signal': score.street_name_signal, 'zip_signal': score.zip_signal,
                    'suite_signal': score.suite_signal, 'address_conflict': conflict,
                    'name_score': round(name_score, 3), 'name_signal': 'MISSING' if not any(cad_names) else 'STRONG' if name_score >= .85 else 'PARTIAL' if name_score >= .6 else 'NO MATCH',
                    'decision': decisions.get(normalize_account_number(prop.real_account_number)),
                    'is_confirmed': bool(confirmed and confirmed['account_number'] == prop.real_account_number)})
        candidates.append(row)
    candidates.sort(key=lambda r: (not r['is_confirmed'], r['address_conflict'], -r['name_score'], r['real_account_number']))
    return {'candidates': candidates[offset:offset+20], 'offset': offset, 'has_more': len(candidates) > offset+20,
            'candidate_count': len(candidates), 'truncated': not account_number and len(records) >= 500,
            'confirmed': confirmed, 'history': history[:30], 'can_review': bool(permit_id),
            'source': {'business_name': item.business_name, 'legal_name': item.legal_name, 'address': item.source_address, 'zip': item.source_zip}}


def save_decision(jurisdiction, item, *, property_record_id: str, decision: str, notes: str | None, reviewed_by: str) -> dict:
    if decision not in ('CONFIRMED', 'REJECTED'):
        raise CadReviewError('Choose Confirm or Reject.')
    if not reviewed_by:
        raise CadReviewError('A signed-in reviewer is required.')
    permit_id = item_permit_id(item)
    if not permit_id:
        raise CadReviewError('This item has no source permit to attach a match decision to.')
    prop = get_property_adapter(jurisdiction).get_property_by_id(jurisdiction, property_record_id)
    if not prop or classify_account_type(prop.real_account_number, prefixes=jurisdiction.account_type_prefixes) != 'PERSONAL':
        raise CadReviewError('Choose a personal-property account from this district.')
    if decision == 'CONFIRMED' and not eligible_for_current_dba_match(prop):
        raise CadReviewError('This account is not in the current active DBA export.')
    score = score_property_candidate(normalize_address(item.source_address, zip_code=item.source_zip), prop)
    conflict = score.suite_conflict or score.zip_signal == 'NO MATCH' or not score.street_number_match or score.street_name_signal not in ('MATCH', 'NORMALIZED MATCH')
    if decision == 'CONFIRMED' and conflict and not (notes or '').strip():
        raise CadReviewError('Explain why this account is correct despite the address or suite difference.')
    rows = _request('POST', 'cad_match_decisions', payload={
        'district_id': item.district_id, 'jurisdiction_id': jurisdiction.id,
        'permit_location_id': permit_id, 'property_record_id': prop.property_id,
        'account_number': prop.real_account_number, 'decision': decision,
        'notes': (notes or '').strip() or None, 'reviewed_by': reviewed_by,
        'source_table': item.source_table, 'source_item_id': item.id,
    })
    return rows[0]


def _count_properties(jurisdiction, **filters):
    url, key = get_supabase_config()
    response = requests.get(f'{url}/rest/v1/real_property_records', headers=postgrest_headers(key, prefer='count=exact'),
                            params={'select': 'id', 'jurisdiction_id': f'eq.{jurisdiction.id}', 'limit': 1, **filters}, timeout=20)
    if response.status_code >= 400:
        raise ComptrollerServiceError('Unable to read CAD data coverage.')
    total = response.headers.get('Content-Range', '').split('/')[-1]
    return int(total) if total.isdigit() else None


def latest_completed_import(jurisdiction_id):
    candidates = []
    for status in ('COMPLETED', 'LEGACY'):
        rows = _request('GET', 'property_source_imports', params={
            'select':'*', 'jurisdiction_id':f'eq.{jurisdiction_id}',
            'import_status':f'eq.{status}', 'row_count':'gte.1',
            'order':'imported_at.desc,id.desc', 'limit':1})
        candidates.extend(rows)
    return max(candidates, key=lambda r:(r['imported_at'], r['id'])) if candidates else None


def match_health(jurisdiction, items, *, today: date | None = None):
    today = today or datetime.now(ZoneInfo(jurisdiction.timezone)).date()
    imports = _request('GET', 'property_source_imports', params={'select': '*', 'jurisdiction_id': f'eq.{jurisdiction.id}', 'order': 'imported_at.desc,id.desc', 'limit': 10})
    latest = latest_completed_import(jurisdiction.id)
    as_of = date.fromisoformat(latest['source_as_of_date']) if latest and latest.get('source_as_of_date') else None
    age = (today - as_of).days if as_of else None
    freshness = 'UNKNOWN' if age is None else 'FUTURE_DATE' if age < 0 else 'STALE' if age > 90 else 'CURRENT'
    prefix = (jurisdiction.account_type_prefixes.get('personal') or '').strip()
    filters = {'real_account_number': f'like.{prefix}*'} if prefix else {}
    filters['and'] = '(or(account_status.is.null,account_status.neq.NOT_IN_ACTIVE_DBA_EXPORT))'
    total = _count_properties(jurisdiction, **filters)
    named = _count_properties(jurisdiction, **filters, **{'or': '(owner_name.not.is.null,business_name.not.is.null,dba_name.not.is.null)'})
    relevant = [i for i in items if i.signal_type in ('new_business', 'sales_tax_inactive')]
    counts = Counter('ambiguous' if i.is_ambiguous else 'unmatched' if i.confidence in (None, 'UNMATCHED') else 'high_confidence' if i.confidence == 'HIGH' else 'possible' for i in relevant)
    url, key = get_supabase_config()
    decisions = _paginated_get(url, postgrest_headers(key), 'cad_match_decisions', {'select':'*', 'jurisdiction_id': f'eq.{jurisdiction.id}', 'order':'reviewed_at.desc,id.desc'},page_size=500)
    permit_groups = {}
    for d in decisions:permit_groups.setdefault(d['permit_location_id'], []).append(d)
    confirmed = sum(bool(current_decisions(rows)[0]) for rows in permit_groups.values())
    return {'latest_import': latest, 'recent_imports': imports, 'freshness': freshness, 'source_age_days': age,
            'stale_after_days': 90, 'bpp_record_count': total if prefix else None,
            'named_bpp_record_count': named if prefix else None,
            'name_coverage_percent': round(100*named/total,1) if prefix and total and named is not None else None,
            'queue': {'total':len(relevant), **{k:counts[k] for k in ('high_confidence','possible','ambiguous','unmatched')}},
            'human_confirmed_permits': confirmed, 'decision_count':len(decisions)}


def refresh_item_match(item, *, candidates=None):
    """Refresh advisory evidence after a decision, without resolving the alert."""
    from app.comptroller.matching import match_closure_to_account
    from app.comptroller.new_business import classify_match
    result = match_closure_to_account(district_id=item.district_id, permit_location_id=item_permit_id(item),
        permit_address=item.source_address, permit_zip=item.source_zip, permit_legal_name=item.legal_name,
        permit_location_name=item.business_name, candidates=candidates)
    if 'lookup failed' in result.reason:
        raise ComptrollerServiceError(result.reason)
    candidate = result.candidate
    common = {'matched_account_number':candidate.account_number if candidate else None, 'matched_owner_name':candidate.owner_name if candidate else None, 'match_reason':result.reason}
    if item.source_table == 'bpp_intelligence_items':
        classification, priority = classify_match(result)
        payload = {**common, 'matched_record_id':candidate.record_id if candidate else None,
                   'confidence':result.confidence, 'confidence_score':result.score, 'is_ambiguous':result.ambiguous,
                   'match_signals':result.signals, 'classification':classification,'priority':priority}
    else:
        payload = {**common, 'matched_account_id':candidate.record_id if candidate else None,
                   'match_confidence':result.confidence,'match_score':result.score,'match_ambiguous':result.ambiguous}
    _request('PATCH',item.source_table,params={'id':f'eq.{item.id}','district_id':f'eq.{item.district_id}'},payload=payload)
    return result


def refresh_after_import(jurisdiction, import_id, quality):
    """Refresh open alerts and save a match-rate snapshot for this import.

    A failed refresh is visible on the import; data import success and
    matching completion are tracked independently. Reviewed alerts stay put.
    """
    from app.comptroller.intelligence import list_intelligence_queue
    from app.comptroller.matching import fetch_candidate_records
    try:
        items = list_intelligence_queue(jurisdiction.district_id)
        pending = [i for i in items if i.signal_type in ('new_business','sales_tax_inactive') and i.status in ('NEW','IN_REVIEW')]
        candidates = fetch_candidate_records(jurisdiction.district_id)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda item: refresh_item_match(item, candidates=candidates), pending))
        items = list_intelligence_queue(jurisdiction.district_id)
        relevant = [i for i in items if i.signal_type in ('new_business','sales_tax_inactive')]
        counts = Counter('ambiguous' if i.is_ambiguous else 'unmatched' if i.confidence in (None,'UNMATCHED') else 'high_confidence' if i.confidence == 'HIGH' else 'possible' for i in relevant)
        quality = {**quality, 'matching_refresh_status':'COMPLETED', 'matching_refreshed_at':datetime.now(timezone.utc).isoformat(),
                   'queue_match_snapshot':{'total':len(relevant),**dict(counts)},'queue_items_refreshed':len(pending)}
    except Exception:
        quality = {**quality,'matching_refresh_status':'FAILED','matching_refresh_message':'CAD import completed, but matching could not finish. Retry the match refresh.'}
    _request('PATCH','property_source_imports',params={'id':f'eq.{import_id}','jurisdiction_id':f'eq.{jurisdiction.id}'},payload={'quality_summary':quality})
