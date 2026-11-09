"""CRS-vs-Comptroller research candidates, never correction instructions.

Writes only observations, queue evidence, comparison state, and run telemetry.
SQL RPCs serialize transitions and preserve concurrent human review decisions.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from datetime import datetime, timezone

from app.comptroller import mailing_address_intelligence as legacy
from app.comptroller.address_normalizer import normalize_mailing_address, extract_unit
from app.comptroller.jurisdictions import get_jurisdiction, validate_capability
from app.comptroller.mailing_account_links import diagnose_account_link
from app.comptroller.mailing_address_matching import compare_mailing_addresses
from app.comptroller.property_adapter import get_property_adapter


def request(method, table, *, params=None, payload=None):
    return legacy._request_json(method, f'{legacy._base_url()}/rest/v1/{table}', legacy._headers(),
                                params=params, json_payload=payload)


def snapshot(line1=None, line2=None, unit=None, city=None, state=None, zip_code=None, recipient=None, **metadata):
    """Preserve all raw fields. Normalization is comparison only, not postal validation."""
    normalized = normalize_mailing_address(line1, city=city, state=state, zip_code=zip_code)
    second = re.sub(r'[^A-Z0-9# ]', ' ', (line2 or '').upper())
    second = ' '.join(second.split())
    second_base, second_unit = extract_unit('X '+second)
    if second_unit and second_base == 'X':
        second = ''  # A suite-only second line is the same as a separate unit column.
    else:
        second_unit = None
    explicit_unit = ' '.join((unit or '').upper().split()) or None
    if explicit_unit:
        # Column may contain either "200" or "STE 200".
        base, parsed = extract_unit('X '+explicit_unit)
        explicit_unit = parsed if base == 'X' and parsed else explicit_unit
    units = {u for u in (normalized.unit, second_unit, explicit_unit) if u}
    reasons = []
    if not normalized.normalized_line:
        reasons.append('Missing street or PO box')
    elif not re.search(r'\d', normalized.normalized_line) or not re.search(r'[A-Z]', normalized.normalized_line):
        reasons.append('Street/PO-box number is unavailable')
    if len(units)>1:
        reasons.append('Conflicting unit fields')
    if not normalized.city:
        reasons.append('Missing city')
    if not normalized.state or not re.fullmatch(r'[A-Z]{2}', normalized.state):
        reasons.append('Missing or unsupported state')
    if not re.fullmatch(r'\d{5}(?:[ -]?\d{4})?', (zip_code or '').strip()):
        reasons.append('Missing or unsupported ZIP')
    normalized = replace(normalized, unit=next(iter(units)) if len(units)==1 else normalized.unit)
    return dict(line1=line1, line2=line2, unit=unit, city=city, state=state, zip=zip_code,
                recipient=recipient, normalized=normalized.full_normalized,
                normalized_line=normalized.normalized_line, normalized_line2=second, normalized_unit=normalized.unit, zip4=normalized.zip4,
                complete=not reasons, incomplete_reasons=reasons, postal_validation='NOT_PERFORMED', **metadata)


def compare_snapshots(crs, comptroller):
    if not crs['line1']:
        return 'MISSING_CRS_MAILING', ['No imported CRS mailing street/PO box; verify in CRS.'], {}
    if not crs['complete'] or not comptroller['complete']:
        reasons = ['CRS: '+x for x in crs['incomplete_reasons']]+['Comptroller: '+x for x in comptroller['incomplete_reasons']]
        return 'INCOMPLETE_DATA', reasons, {}
    result = compare_mailing_addresses(
        current_raw=crs['line1'], current_city=crs['city'], current_state=crs['state'], current_zip=crs['zip'],
        observed_raw=comptroller['line1'], observed_city=comptroller['city'], observed_state=comptroller['state'], observed_zip=comptroller['zip'])
    differences = dict(result.differences)
    # Respect units on second lines or separate columns, not just line 1.
    differences['unit'] = 'SAME' if crs['normalized_unit']==comptroller['normalized_unit'] else 'CHANGED'
    differences['line2'] = 'SAME' if crs['normalized_line2']==comptroller['normalized_line2'] else 'CHANGED'
    material = [key for key,value in differences.items() if value=='CHANGED' and key not in ('zip4','unit','line2')]
    zip4_changed = bool(crs['zip4'] and comptroller['zip4'] and crs['zip4'] != comptroller['zip4'])
    if material or zip4_changed or differences['unit']=='CHANGED' or differences['line2']=='CHANGED':
        return 'DISCREPANCY', [key.replace('_',' ')+' differs' for key,value in differences.items() if value=='CHANGED' and (key!='zip4' or zip4_changed)], differences
    return 'NO_CURRENT_DISCREPANCY', ['No material address discrepancy after normalization.'], differences


def fingerprint(crs, comptroller, status):
    # Exclude retrieval/import timestamps: a fresh copy of the same facts
    # must not reopen completed reviews. Include missingness and line 2.
    fields = ('normalized','normalized_line2','complete','incomplete_reasons')
    value = [status, {k:crs[k] for k in fields}, {k:comptroller[k] for k in fields}]
    if crs['zip4'] and comptroller['zip4'] and crs['zip4'] != comptroller['zip4']:
        value.extend([crs['zip4'],comptroller['zip4']])
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _crs_snapshot(prop):
    return snapshot(prop.mailing_address_line1, prop.mailing_address_line2, prop.mailing_unit,
                    prop.mailing_city, prop.mailing_state, prop.mailing_zip, prop.mailing_recipient,
                    source=prop.mailing_source or 'imported_crs_export', import_id=prop.mailing_import_id,
                    imported_at=prop.mailing_imported_at, source_as_of_date=prop.mailing_source_as_of_date,
                    tax_year=prop.tax_year, field_sources=prop.mailing_field_sources or {})


def latest_run(district_id):
    rows = request('GET','mailing_scan_runs',params={'district_id':f'eq.{district_id}',
                   'select':'*','order':'started_at.desc,id.desc','limit':1})
    return rows[0] if rows else None


def open_comparison_accounts(jurisdiction):
    result={};offset=0
    while True:
        rows=request('GET','bpp_intelligence_items',params={'select':'source_taxpayer_id,matched_account_number,status,mailing_comparison_key',
            'jurisdiction_id':f'eq.{jurisdiction.id}','signal_type':'eq.mailing_address_change',
            'status':'in.(NEW,IN_REVIEW)','mailing_comparison_key':'not.is.null','order':'id.asc','limit':1000,'offset':offset})
        if not rows:break
        for row in rows:
            if row.get('mailing_comparison_key') and row['status'] in ('NEW','IN_REVIEW'):
                result.setdefault(row['source_taxpayer_id'],set()).add(row.get('matched_account_number'))
        offset+=len(rows)
    return result


def run_scan(jurisdiction_id, *, dry_run=False):
    jurisdiction = get_jurisdiction(jurisdiction_id)
    validation = validate_capability(jurisdiction, legacy.CAPABILITY, frozenset({'mailing_address'}))
    if not validation.ok:
        raise legacy.MailingAddressIntelligenceError(validation.message)
    run_id = None
    if not dry_run:
        run_id = request('POST','rpc/mailing_start_scan',payload={
            'p_jurisdiction_id':jurisdiction.id, 'p_district_id':jurisdiction.district_id, 'p_county':jurisdiction.county_name})
    counts = dict(evaluated=0, same_address=0, format_only=0, possible_change=0, likely_change=0,
                  insufficient_data=0, baseline_established=0, items_created=0, items_updated=0,
                  duplicates_suppressed=0, observations_added=0, unlinked=0, missing_crs=0)
    errors=[]; item_ids=[]
    def finish(status):
        if run_id:
            request('PATCH','mailing_scan_runs',params={'id':f'eq.{run_id}','jurisdiction_id':f'eq.{jurisdiction.id}','status':'eq.RUNNING'},payload={
                'status':status,'finished_at':datetime.now(timezone.utc).isoformat(), 'records_examined':counts['evaluated'],
                'observations_added':counts['observations_added'],'flags_created':counts['items_created'],
                'flags_updated':counts['items_updated'],'unlinked':counts['unlinked'],'missing_crs':counts['missing_crs'],
                'incomplete_source':counts['insufficient_data'],'errors':errors})
    try:
        candidates = legacy.get_mailing_address_candidates(jurisdiction)
        adapter = get_property_adapter(jurisdiction)
        existing_accounts = open_comparison_accounts(jurisdiction)
        for candidate in candidates:
            source_zip = candidate.mailing_zip
            if source_zip and candidate.mailing_zip4 and len(re.sub(r'\D','',source_zip)) == 5:
                source_zip += '-'+candidate.mailing_zip4
            if run_id and counts['evaluated'] % 25 == 0:
                request('POST','rpc/mailing_assert_run',payload={'p_run_id':run_id,'p_jurisdiction_id':jurisdiction.id})
                request('PATCH','mailing_scan_runs',params={'id':f'eq.{run_id}','status':'eq.RUNNING'},payload={
                    'heartbeat_at':datetime.now(timezone.utc).isoformat(), 'records_examined':counts['evaluated'],
                    'observations_added':counts['observations_added'],'flags_created':counts['items_created'],
                    'flags_updated':counts['items_updated'],'unlinked':counts['unlinked'],
                    'missing_crs':counts['missing_crs'],'incomplete_source':counts['insufficient_data'],'errors':errors})
            counts['evaluated']+=1
            try:
                observed = snapshot(candidate.mailing_address, city=candidate.mailing_city, state=candidate.mailing_state,
                    zip_code=source_zip, recipient=candidate.legal_name,
                    source=legacy.COMPTROLLER_SOURCE, retrieved_at=candidate.retrieved_at, source_effective_date=None)
                if candidate.conflicting_addresses:
                    observed.update(complete=False, incomplete_reasons=['Conflicting taxpayer addresses across active locations'],
                                    conflicting_observations=list(candidate.address_variants))
                if observed['complete'] and not dry_run:
                    result = legacy.record_observation(jurisdiction, account_identifier_type='comptroller_taxpayer',
                        account_identifier=candidate.taxpayer_id, source=legacy.COMPTROLLER_SOURCE, source_record_id=candidate.taxpayer_id,
                        source_effective_date=None,raw_line=candidate.mailing_address,city=candidate.mailing_city,
                        state=candidate.mailing_state,zip_code=source_zip,run_id=run_id)
                    counts['observations_added']+=int(result['added'])
                if not observed['complete']:
                    counts['insufficient_data']+=1
                diagnostic = diagnose_account_link(jurisdiction,candidate.taxpayer_id)
                link = diagnostic['link']
                current_account=link.get('matched_account_number')
                if not dry_run and existing_accounts.get(candidate.taxpayer_id,set()) - {current_account}:
                    counts['items_updated']+=request('POST','rpc/mailing_mark_unreliable',payload={
                        'p_jurisdiction_id':jurisdiction.id,'p_taxpayer_id':candidate.taxpayer_id,
                        'p_current_account':current_account,'p_diagnostic':{**diagnostic,'scan_run_id':run_id}})
                if not link['matched_record_id']:
                    counts['unlinked']+=1
                    continue
                prop = adapter.get_property_by_id(jurisdiction,link['matched_record_id'])
                if not prop:
                    raise legacy.MailingAddressIntelligenceError('Linked property unavailable')
                crs = _crs_snapshot(prop)
                status, reasons, differences = compare_snapshots(crs,observed)
                if status=='MISSING_CRS_MAILING': counts['missing_crs']+=1
                elif status=='DISCREPANCY': counts['likely_change']+=1
                elif status=='NO_CURRENT_DISCREPANCY': counts['same_address']+=1
                actionable = status in ('DISCREPANCY','MISSING_CRS_MAILING','INCOMPLETE_DATA')
                payload = dict(district_id=jurisdiction.district_id, source_taxpayer_id=candidate.taxpayer_id,
                    business_name=candidate.legal_name,legal_name=candidate.legal_name, **link,
                    classification='LIKELY_MAILING_ADDRESS_CHANGE' if status=='DISCREPANCY' else 'POSSIBLE_MAILING_ADDRESS_CHANGE',
                    confidence=diagnostic['identity_confidence'], priority='HIGH' if status=='DISCREPANCY' else 'MEDIUM',
                    mailing_address_current=crs['normalized'] if crs['line1'] else None, mailing_address_observed=observed['normalized'] or None,
                    match_reason='; '.join(reasons),match_signals=differences,
                    recommended_action=('Research both sources; neither is established as the correct mailing address.' if status=='DISCREPANCY'
                        else 'No supported correction. Verify missing/incomplete data or confirm the current sources agree.'),
                    evidence={'scan_run_id':run_id,'comparison_version':2,'comparison_status':status,'crs':crs,'comptroller':observed,
                              'account_match':diagnostic,'identity_confidence':diagnostic['identity_confidence'],
                              'postal_validation':'NOT_PERFORMED','evaluated_at':datetime.now(timezone.utc).isoformat(),
                              'discrepancy_reasons':reasons, 'correction_supported':False})
                if dry_run: continue
                key = candidate.taxpayer_id.strip()+':'+link['matched_account_number'].strip().upper()
                saved = request('POST','rpc/mailing_save_comparison',payload={'p_jurisdiction_id':jurisdiction.id,
                    'p_key':key,'p_fingerprint':fingerprint(crs,observed,status),'p_actionable':actionable,'p_payload':payload})
                action=saved['action']
                if action=='CREATED':counts['items_created']+=1
                elif action=='UPDATED':counts['items_updated']+=1
                elif action=='SUPPRESSED':counts['duplicates_suppressed']+=1
                if action in ('CREATED','UPDATED'):item_ids.append(saved['item_id'])
            except Exception as exc:
                # No recipient/address/provider response bodies in telemetry.
                errors.append({'record_key':hashlib.sha256(candidate.taxpayer_id.encode()).hexdigest()[:12],
                               'error_type':type(exc).__name__})
        finish('FAILED' if errors else 'SUCCESS')
    except Exception as exc:
        errors.append({'error_type':type(exc).__name__})
        finish('FAILED')
        raise
    if errors:
        raise legacy.MailingAddressIntelligenceError(f'Mailing scan recorded {len(errors)} record failures; see run {run_id}.')
    return legacy.MailingAddressIntelligenceResult(jurisdiction.id,dry_run,**counts,item_ids=item_ids,run_id=run_id)
