"""District allocation policies; existing queue assigned_to fields remain authoritative."""
from collections import defaultdict
from datetime import datetime,timezone
from app.comptroller.service import _request_json,get_supabase_config,postgrest_headers
from app.workspace_access import read_access,district_access
from app.login_aliases import DEMO_AUTH_EMAIL,DEMO_DISTRICT_ID


def request(method,path,*,params=None,payload=None):
    url,key=get_supabase_config()
    return _request_json(method,url+'/rest/v1/'+path,postgrest_headers(key,prefer='return=representation'),params=params,json_payload=payload)


def eligible_roster(district_id):
    if district_id==DEMO_DISTRICT_ID or not district_access(district_id)['bpp']:return []
    rows=request('GET','district_users',params={'district_id':'eq.'+district_id,'select':'user_id,email,full_name,role','limit':1000})
    result={}
    for row in rows:
        if not row.get('user_id') or row['email'].strip().lower()==DEMO_AUTH_EMAIL:continue
        if row.get('role')!='admin' and not read_access(district_id,row['email'])['bpp']:continue
        result[row['user_id']]={'id':row['user_id'],'label':row.get('full_name') or row['email'],'email':row['email']}
    return sorted(result.values(),key=lambda x:x['id'])


def configure(context,enabled):
    roster=eligible_roster(context.district_id)
    allocated=request('POST','rpc/bpp_configure_assignments',payload={'p_district_id':context.district_id,'p_actor_id':context.user_id,'p_enabled':enabled,'p_roster':roster})
    return {'enabled':enabled,'allocated_items':allocated,'appraisers':roster}


def status(context):
    if context.district_id==DEMO_DISTRICT_ID:return {'enabled':False,'appraisers':[],'unassigned_accounts':0}
    rows=request('GET','bpp_assignment_settings',params={'district_id':'eq.'+context.district_id,'select':'*','limit':1})
    enabled=bool(rows and rows[0]['enabled'])
    roster=eligible_roster(context.district_id)
    if enabled and rows[0]['roster']!=roster:
        # Access changes affect future allocation, without rewriting review ownership.
        request('PATCH','bpp_assignment_settings',params={'district_id':'eq.'+context.district_id},payload={'roster':roster,'updated_at':datetime.now(timezone.utc).isoformat()})
        request('POST','rpc/bpp_balance_pending',payload={'p_district_id':context.district_id})
    counts=defaultdict(set);unassigned=set();offset=0
    while True:
        page=request('GET','bpp_active_assignment_work',params={'district_id':'eq.'+context.district_id,'select':'work_key,assigned_to,busy','order':'source_table,id','limit':1000,'offset':offset})
        for row in page:
            if row['assigned_to']:counts[row['assigned_to']].add(row['work_key'])
            else:unassigned.add(row['work_key'])
        offset+=len(page)
        if len(page)<1000:break
    return {'enabled':enabled,'appraisers':[{**person,'open_accounts':len(counts[person['id']])} for person in roster],
            'unassigned_accounts':len(unassigned),'unavailable_assignee_accounts':sum(len(v) for k,v in counts.items() if k not in {r['id'] for r in roster})}


def reassign(context,item,target):
    if target not in {r['id'] for r in eligible_roster(context.district_id)}:raise ValueError('Choose a current BPP appraiser in your district.')
    from app.bpp_work_claims import work_key
    return request('POST','rpc/bpp_reassign_account',payload={'p_district_id':context.district_id,'p_actor_id':context.user_id,'p_work_key':work_key(item),'p_target':target})
