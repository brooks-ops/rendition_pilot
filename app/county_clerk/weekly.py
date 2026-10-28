"""Monday source-access check, invoked by the existing daily intelligence job.

This does not retrieve public pages. A verified authorized adapter must replace
EagleWebConnector's hard gate before unattended collection can be activated.
"""
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from .connector import EagleWebConnector, RetrievalNotAuthorized, next_window
from .service import request


def check_weekly(jurisdiction, *, dry_run=False, now=None):
    now=now or datetime.now(ZoneInfo('America/Chicago'))
    if now.astimezone(ZoneInfo('America/Chicago')).weekday()!=0:
        return {'status':'NOT_DUE','cadence':'weekly','weekday':'Monday'}
    sources=request('GET','county_clerk_sources',params={'select':'*','district_id':'eq.'+jurisdiction.district_id,'jurisdiction_id':'eq.'+jurisdiction.id,'limit':2})
    if not sources:return {'status':'NOT_CONFIGURED'}
    if len(sources)!=1:raise ValueError('Ambiguous County Clerk source configuration')
    source=sources[0]
    result={'status':'BLOCKED','county':source['county'],'cadence':'weekly','checked_at':now.isoformat(),'window':next_window(source,through=now.date()),'records_retrieved':0}
    if dry_run:return {**result,'status':'DRY_RUN','message':'Source retrieval remains disabled.'}
    # Even a mistakenly enabled flag cannot bypass the connector's hard gate.
    try:EagleWebConnector(source).retrieve_window(result['window']['start'],result['window']['end'])
    except RetrievalNotAuthorized as exc:result['message']=str(exc)
    else:raise RuntimeError('Collection adapter requires an explicitly implemented ingestion path')
    print('[county-clerk-weekly] '+json.dumps(result),flush=True)
    return result


def main():
    from app.comptroller.jurisdictions import get_jurisdiction_by_district_id
    now=datetime.now(ZoneInfo('America/Chicago'))
    if now.weekday()!=0:
        print('[county-clerk-weekly] '+json.dumps({'status':'NOT_DUE','cadence':'weekly'}))
        return 0
    sources=request('GET','county_clerk_sources',params={'select':'district_id,jurisdiction_id','limit':1000})
    errors=0
    for source in sources:
        try:
            j=get_jurisdiction_by_district_id(source['district_id'])
            if not j or j.id!=source['jurisdiction_id']:raise ValueError('District/source mismatch')
            check_weekly(j,now=now)
        except Exception as exc:
            errors+=1
            print('[county-clerk-weekly] '+json.dumps({'status':'FAILED','district_id':source['district_id'],'error_type':type(exc).__name__}),flush=True)
    return int(bool(errors))

if __name__=='__main__':
    raise SystemExit(main())
