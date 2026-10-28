"""Persistent, district-scoped reservations for BPP account reviews."""
from app.comptroller.service import _request_json, get_supabase_config, postgrest_headers

class WorkConflict(RuntimeError):
    pass


def work_key(item):
    account = str(item.matched_account_number or '').strip().upper()
    return 'account:' + account if account else f'item:{item.source_table}:{item.id}'


def request(method, path, **kwargs):
    url, key = get_supabase_config()
    return _request_json(method, url+'/rest/v1/'+path, postgrest_headers(key), **kwargs)


def claims(district_id):
    result=[]
    for offset in range(0,100000,1000):
        rows=request('GET','bpp_work_claims',params={'district_id':'eq.'+district_id,'select':'*','order':'work_key','offset':offset,'limit':1000})
        result.extend(rows)
        if len(rows)<1000:break
    active={row['work_key']:row for row in result}
    for row in result:active.setdefault(f"item:{row['source_table']}:{row['item_id']}",row)
    return active


def view(claim, context):
    if not claim:return None
    return {**{k:claim[k] for k in ('actor_email','claimed_at')},'mine':claim['actor_id']==context.user_id,
            'can_release':claim['actor_id']==context.user_id or context.role=='admin'}


def claim(context,item):
    row=request('POST','rpc/bpp_claim_work',json_payload={'p_district_id':context.district_id,'p_work_key':work_key(item),
        'p_actor_id':context.user_id,'p_actor_email':context.email,'p_source_table':item.source_table,'p_item_id':item.id})
    if not row or row['actor_id']!=context.user_id:
        raise WorkConflict('This account is being worked by '+str((row or {}).get('actor_email') or 'another appraiser')+'. Choose another account or ask them to release it.')
    return row


def release(context,item,*,admin=False):
    existing=claims(context.district_id)
    owner=existing.get(work_key(item)) or existing.get(f'item:{item.source_table}:{item.id}')
    if owner and owner['actor_id']!=context.user_id and not (admin and context.role=='admin'):
        raise WorkConflict('Only the assigned appraiser or a district admin can release this account.')
    params={'district_id':'eq.'+context.district_id,'work_key':'eq.'+work_key(item)}
    if not (admin and context.role=='admin'):params['actor_id']='eq.'+context.user_id
    params.pop('work_key')
    # Release the original unmatched reservation as well if the CAD match changed.
    params['or']=f'(work_key.eq.{work_key(item)},and(source_table.eq.{item.source_table},item_id.eq.{item.id}))'
    request('DELETE','bpp_work_claims',params=params)


def attach(items,context):
    active=claims(context.district_id)
    result=[]
    for item in items:
        key='account:'+str(item['matched_account_number']).strip().upper() if item.get('matched_account_number') else f"item:{item['source_table']}:{item['id']}"
        row=active.get(key) or active.get(f"item:{item['source_table']}:{item['id']}")
        result.append({**item,'work_claim':view(row,context)})
    return result
