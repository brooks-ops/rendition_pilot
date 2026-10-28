"""Run due quarterly YTD collections from the existing daily server job."""
import json,time
from tempfile import TemporaryDirectory
from pathlib import Path
from .connector import EagleWebConnector,public_enabled
from .import_captured import ingest
from .public_client import SourceUnavailable
from .schedule import schedule
from .service import request


def check_quarterly(jurisdiction, *, dry_run=False, now=None):
    plan = schedule(now)
    sources = request('GET', 'county_clerk_sources', params={
        'select': '*', 'district_id': 'eq.' + jurisdiction.district_id,
        'jurisdiction_id': 'eq.' + jurisdiction.id, 'limit': 2})
    if not sources:
        return {'status': 'NOT_CONFIGURED', 'cadence': 'quarterly'}
    if len(sources) != 1:
        raise ValueError('Ambiguous County Clerk source configuration')
    source = sources[0]
    result = {'status': 'DRY_RUN' if dry_run else 'BLOCKED', 'county': source['county'],
              'cadence': 'quarterly', 'window': plan['pending_window']}
    if dry_run:
        return result
    # Atomic unique source/date key: repeated county commands cannot duplicate a plan.
    run = request('POST', 'rpc/dba_plan_quarterly_run', payload={
        'p_source_id': source['id'], 'p_scheduled_for': plan['pending_window']['scheduled_for']})
    if run['district_id'] != jurisdiction.district_id or run['jurisdiction_id'] != jurisdiction.id:
        raise ValueError('Collection plan belongs to another district')
    result.update(status=run['status'], run_id=run['id'],
                  records_retrieved=run.get('records_retrieved'), message=run.get('message'))
    if public_enabled(source):
        claim=request('POST','rpc/dba_claim_collection',payload={'p_run_id':run['id']})
        if claim.get('claimed'):
            lease=claim['run'];last_heartbeat=0.
            def heartbeat():
                nonlocal last_heartbeat
                if time.monotonic()-last_heartbeat<60:return
                ok=request('POST','rpc/dba_collection_heartbeat',payload={'p_run_id':run['id'],'p_token':lease['lease_token']})
                if not ok:raise RuntimeError('Collection lease lost')
                last_heartbeat=time.monotonic()
            try:
                with TemporaryDirectory(prefix='rendition-dba-') as directory:
                    EagleWebConnector(source).retrieve_window(run['window_start'],run['window_end'],Path(directory),heartbeat=heartbeat)
                    heartbeat()
                    imported=ingest(Path(directory),source['district_id'],run['window_start'],run['window_end'],commit=True,collection_method='public_guest_html')
                run=request('POST','rpc/dba_finish_collection',payload={'p_run_id':run['id'],'p_token':lease['lease_token'],'p_import_id':imported.get('import_id') or imported['id']})
            except Exception as exc:
                # Never store credentials or source response bodies in run errors.
                error=('STOP: '+str(exc)) if isinstance(exc,SourceUnavailable) else ('Collection failed: '+type(exc).__name__+'. Retried at most three times, at least 24 hours apart.')
                run=request('POST','rpc/dba_finish_collection',payload={'p_run_id':run['id'],'p_token':lease['lease_token'],'p_error':error})
                print('[county-clerk-quarterly] collection error: '+type(exc).__name__,flush=True)
            result.update(status=run['status'],records_retrieved=run.get('records_retrieved'),message=run.get('message'))
    print('[county-clerk-quarterly] ' + json.dumps(result), flush=True)
    return result


def main():
    from app.comptroller.jurisdictions import get_jurisdiction_by_district_id
    sources = request('GET', 'county_clerk_sources', params={
        'select': 'district_id,jurisdiction_id', 'limit': 1000})
    errors = 0
    for source in sources:
        try:
            jurisdiction = get_jurisdiction_by_district_id(source['district_id'])
            if not jurisdiction or jurisdiction.id != source['jurisdiction_id']:
                raise ValueError('District/source mismatch')
            outcome=check_quarterly(jurisdiction)
            if outcome["status"]=="FAILED":errors+=1
        except Exception as exc:
            errors += 1
            print('[county-clerk-quarterly] ' + json.dumps({
                'status': 'FAILED', 'district_id': source['district_id'],
                'error_type': type(exc).__name__}), flush=True)
    return int(bool(errors))


if __name__ == '__main__':
    raise SystemExit(main())
