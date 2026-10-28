"""Plan quarterly YTD collections using the existing daily server job.

No source-network client is enabled. A durable BLOCKED plan keeps missing
coverage visible and catches a missed scheduled day on the next daily run.
"""
import json
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
    # No supported, authorized collector has been confirmed. Planning cannot enable it.
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
            check_quarterly(jurisdiction)
        except Exception as exc:
            errors += 1
            print('[county-clerk-quarterly] ' + json.dumps({
                'status': 'FAILED', 'district_id': source['district_id'],
                'error_type': type(exc).__name__}), flush=True)
    return int(bool(errors))


if __name__ == '__main__':
    raise SystemExit(main())
