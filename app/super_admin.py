"""Owner-only, read-only cross-district launch review."""
from datetime import datetime,timezone,timedelta
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import UUID
import requests
from fastapi import APIRouter,HTTPException
from pydantic import BaseModel
from app.comptroller.service import get_supabase_config,postgrest_headers,_request_json,_paginated_get
from app.workspace_access import district_access,access_for
from app.commercial.remote import object_request

CLAIM='rendition_super_admin'
class Session(BaseModel):
    access_token:str
class DistrictReview(Session):
    district_id:UUID

def require_super_admin(get_user,token):
    user=get_user(token)
    # user_metadata and district admin roles are intentionally not trusted.
    metadata=user.get('app_metadata') or {}
    if metadata.get(CLAIM) is not True:raise HTTPException(403,'Super Admin access is required.')
    if metadata.get('must_change_password'):raise HTTPException(403,'Change your temporary password before continuing.')
    return user

def read(table,**params):
    url,key=get_supabase_config()
    return _request_json('GET',url+'/rest/v1/'+table,postgrest_headers(key),params=params)

def count(table,**filters):
    url,key=get_supabase_config()
    r=requests.get(url+'/rest/v1/'+table,headers=postgrest_headers(key,prefer='count=exact'),params={'select':'id','limit':1,**filters},timeout=20)
    r.raise_for_status();total=r.headers.get('Content-Range','').split('/')[-1]
    if not total.isdigit():raise ValueError('Record count unavailable')
    return int(total)

def login_status(member):
    if not member.get('user_id'):return {'state':'Not created','last_sign_in_at':None}
    url,key=get_supabase_config()
    r=requests.get(url+'/auth/v1/admin/users/'+member['user_id'],headers=postgrest_headers(key),timeout=20)
    r.raise_for_status();u=r.json()
    if u.get('email','').lower()!=member['email'].lower():return {'state':'Account link needs review','last_sign_in_at':None}
    banned=u.get('banned_until');is_banned=False
    if banned:is_banned=datetime.fromisoformat(banned.replace('Z','+00:00'))>datetime.now(timezone.utc)
    state='Disabled' if is_banned else 'Email unconfirmed' if not u.get('email_confirmed_at') else 'Temporary password — first login required' if (u.get('app_metadata') or {}).get('must_change_password') else 'Active' if u.get('last_sign_in_at') else 'Ready — not signed in yet'
    return {'state':state,'last_sign_in_at':u.get('last_sign_in_at')}

def review(district_id):
    rows=read('districts',select='id,name,slug,county,state,domain,active',id='eq.'+district_id,limit=1)
    if not rows:raise HTTPException(404,'District not found.')
    district=rows[0];checks=[];errors=[]
    def check(label,passed,detail):checks.append({'label':label,'passed':bool(passed),'detail':detail})
    check('District active',district['active'],'Active' if district['active'] else 'Retired / inactive')
    grants=district_access(district_id)
    members=read('district_users',select='email,full_name,role,active,user_id',district_id='eq.'+district_id,order='created_at.asc',limit=1000)
    def decorate(member):
        try:login=login_status(member)
        except (requests.RequestException,ValueError):login={'state':'Unable to verify','last_sign_in_at':None};errors.append('A user login could not be verified.')
        return {**{k:v for k,v in member.items() if k!='user_id'},'workspace_access':access_for(SimpleNamespace(district_id=district_id,email=member['email'],role=member['role'])),'login':login}
    with ThreadPoolExecutor(max_workers=4) as pool:users=list(pool.map(decorate,members))
    admins=[m for m in users if m.get('active',True) and m['role']=='admin']
    check('District administrator',admins and any(m['login']['state'] not in ('Not created','Disabled','Email unconfirmed','Unable to verify','Account link needs review') for m in admins),f'{len(admins)} active administrator(s)')
    check('User logins',users and all(m['login']['state'] not in ('Not created','Disabled','Email unconfirmed','Unable to verify','Account link needs review') for m in users if m.get('active',True)),f'{len(users)} authorized user(s); review their login status below')
    duplicates=read('districts',select='id,name',domain='eq.'+district['domain'],active='eq.true') if district.get('domain') else []
    check('District identity',len(duplicates)<=1,'No duplicate active domain' if len(duplicates)<=1 else 'Multiple active districts share this email domain')
    result={'district':district,'workspace_access':grants,'users':users,'checks':checks,'errors':errors,'jurisdiction':None,'latest_sync':None,'latest_import':None,'latest_attempt':None,'permit_count':None,'account_count':None,'queue':[]}
    if grants['bpp']:
        js=read('jurisdictions',select='id,name,county_name,comptroller_county_code,capabilities,property_field_mapping,account_type_prefixes,active',district_id='eq.'+district_id,limit=2)
        check('BPP county configuration',len(js)==1 and js[0]['active'],'One active jurisdiction required')
        if len(js)==1:
            j=js[0];result['jurisdiction']=j
            with ThreadPoolExecutor(max_workers=4) as pool:
                work={
                  'latest_sync':pool.submit(read,'comptroller_sync_runs',select='status,run_type,permits_checked,finished_at,started_at',county='eq.'+j['county_name'],order='started_at.desc',limit=1),
                  'latest_attempt':pool.submit(read,'property_source_imports',select='id,source_as_of_date,imported_at,row_count,import_status,quality_summary',jurisdiction_id='eq.'+j['id'],order='imported_at.desc',limit=1),
                  'latest_import':pool.submit(read,'property_source_imports',select='id,source_as_of_date,imported_at,row_count,import_status,quality_summary',jurisdiction_id='eq.'+j['id'],import_status='in.(COMPLETED,LEGACY)',order='imported_at.desc',limit=1),
                  'permit_count':pool.submit(count,'comptroller_permit_locations',district_id='eq.'+district_id,county='eq.'+j['county_name']),
                  'account_count':pool.submit(count,'real_property_records',jurisdiction_id='eq.'+j['id']),
                  'queue':pool.submit(read,'bpp_intelligence_items',signal_type='neq.county_clerk_dba',select='id,business_name,signal_type,status,priority,confidence,matched_account_number,created_at',district_id='eq.'+district_id,order='created_at.desc',limit=20)}
                for name,future in work.items():
                    try:
                        value=future.result();result[name]=(value[0] if value else None) if name.startswith('latest_') else value
                    except Exception:errors.append(name.replace('_',' ')+' could not be verified.')
            sync=result['latest_sync'];recent=False
            if sync and sync.get('finished_at'):
                recent=datetime.fromisoformat(sync['finished_at'].replace('Z','+00:00'))>datetime.now(timezone.utc)-timedelta(hours=36)
            check('Sales-tax permit sync',bool(j.get('comptroller_county_code')) and sync and sync['status']=='SUCCESS' and recent and (result['permit_count'] or 0)>0,'Last completed sync must be successful and within 36 hours')
            imp=result['latest_import'];attempt=result['latest_attempt']
            check('CAD account export',imp and (result['account_count'] or 0)>0 and j.get('capabilities',{}).get('real_property_linkage'), 'Account export loaded' if imp else 'District administrator still needs to import the account export')
            check('Most recent import attempt',attempt and attempt['import_status'] in ('COMPLETED','LEGACY'),'No incomplete or failed import should be left unresolved')
            if imp:
                as_of=imp.get('source_as_of_date');fresh=False
                if as_of:
                    age=(datetime.now(timezone.utc).date()-datetime.fromisoformat(as_of).date()).days;fresh=0<=age<=90
                check('Account export freshness',fresh,'Export as-of date must be today or within the previous 90 days')
                matching=(imp.get('quality_summary') or {}).get('matching_refresh_status')
                check('CAD match refresh',matching=='COMPLETED',matching or 'No completed match-refresh status recorded')
    if grants['commercial']:
        r=object_request('GET',district_id,'manifest.json')
        check('Commercial models',r.status_code==200,'Commercial workspace imported' if r.status_code==200 else 'Commercial models have not been imported')
    result['ready']=bool(checks) and all(c['passed'] for c in checks) and not errors
    result['checked_at']=datetime.now(timezone.utc).isoformat()
    return result

def router(get_user):
    api=APIRouter(prefix='/api/super-admin')
    @api.post('/districts')
    def districts(request:Session):
        require_super_admin(get_user,request.access_token)
        url,key=get_supabase_config()
        return {'districts':_paginated_get(url,postgrest_headers(key),'districts',{'select':'id,name,county,domain,active','order':'name.asc'},page_size=500)}
    @api.post('/review')
    def district_review(request:DistrictReview):
        require_super_admin(get_user,request.access_token)
        return review(str(request.district_id))
    return api
