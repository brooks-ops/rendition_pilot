"""Link mailing changes using business situs evidence, never mailing-as-situs."""
from concurrent.futures import ThreadPoolExecutor
from app.comptroller.service import get_supabase_config, postgrest_headers, _request_json, _paginated_get


def diagnose_account_link(jurisdiction, taxpayer_id):
    empty = {'matched_record_id':None,'matched_account_number':None,'matched_owner_name':None}
    if not taxpayer_id or not jurisdiction.has_capability('real_property_linkage'):
        return {'link': empty, 'reason': 'NO_LINKAGE_CAPABILITY', 'locations': []}
    url,key=get_supabase_config()
    locations=_request_json('GET',f'{url}/rest/v1/comptroller_permit_locations',postgrest_headers(key),params={
        'select':'id,address,zip,legal_name,location_name','county':f'eq.{jurisdiction.county_name}',
        'taxpayer_id':f'eq.{taxpayer_id}','current_status':'eq.ACTIVE','order':'id.asc','limit':501})
    if len(locations)>500:
        return {'link': empty, 'reason': 'TRUNCATED_PORTFOLIO', 'locations': []}
    from app.comptroller.matching import match_imported_bpp_accounts
    matches={}
    uncertain=False
    evidence=[]
    for location in locations:
        match=match_imported_bpp_accounts(district_id=jurisdiction.district_id,
            address=location.get('address'),zip_code=location.get('zip'),
            legal_name=location.get('legal_name'),business_name=location.get('location_name'),
            permit_location_id=location['id'])
        if match and match.ambiguous:
            uncertain=True
        evidence.append({'permit_location_id': location['id'], 'situs': location.get('address'),
                         'confidence': match.confidence if match else 'UNMATCHED',
                         'ambiguous': bool(match and match.ambiguous), 'score': match.score if match else None,
                         'account': match.candidate.account_number if match and match.candidate else None,
                         'reason': match.reason if match else 'No eligible situs/name candidate',
                         'signals': match.signals if match else {}})
        if match and match.confidence=='HIGH' and not match.ambiguous and match.candidate:
            matches[match.candidate.record_id]=match.candidate
    # Multiple locations resolving to the same account are fine. Different
    # accounts require review; never attach one account arbitrarily.
    if len(matches)!=1 or uncertain:
        reason = ('AMBIGUOUS_LOCATION' if uncertain else 'MULTIPLE_ACCOUNTS' if len(matches)>1
                  else 'NO_ACTIVE_LOCATIONS' if not locations else 'NO_HIGH_CONFIDENCE_MATCH')
        return {'link': empty, 'reason': reason, 'locations': evidence}
    candidate=next(iter(matches.values()))
    return {'link': {'matched_record_id':candidate.record_id,'matched_account_number':candidate.account_number,
                    'matched_owner_name':candidate.owner_name}, 'reason': 'RELIABLE_ACCOUNT_MATCH',
            'identity_confidence': 'HIGH', 'locations': evidence}


def account_link(jurisdiction, taxpayer_id):
    return diagnose_account_link(jurisdiction, taxpayer_id)['link']


def refresh_mailing_account_links(jurisdiction):
    url,key=get_supabase_config();headers=postgrest_headers(key)
    rows=_paginated_get(url,headers,'bpp_intelligence_items',{
        'select':'id,source,status,mailing_comparison_key,source_taxpayer_id,matched_record_id,matched_account_number,matched_owner_name',
        'district_id':f'eq.{jurisdiction.district_id}','signal_type':'eq.mailing_address_change','order':'id.asc'},page_size=500)
    # Completed reviews and new comparison items retain the account/evidence
    # reviewed by staff. New comparisons are refreshed atomically by the scan.
    rows=[r for r in rows if r.get('status') in ('NEW','IN_REVIEW') and not r.get('mailing_comparison_key') and r.get('source')!='returned_mail_scan']
    taxpayer_ids={r.get('source_taxpayer_id') for r in rows}
    with ThreadPoolExecutor(max_workers=4) as pool:
        links=dict(zip(taxpayer_ids,pool.map(lambda tid:account_link(jurisdiction,tid),taxpayer_ids)))
    for row in rows:
        link=links[row.get('source_taxpayer_id')]
        if any(row.get(k)!=v for k,v in link.items()):
            _request_json('PATCH',f'{url}/rest/v1/bpp_intelligence_items',headers,
                params={'id':f"eq.{row['id']}",'district_id':f'eq.{jurisdiction.district_id}'},json_payload=link)
    return {'evaluated':len(rows),'matched':sum(bool(links[r.get('source_taxpayer_id')]['matched_record_id']) for r in rows)}
