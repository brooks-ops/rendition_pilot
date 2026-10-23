"""Link mailing changes using business situs evidence, never mailing-as-situs."""
from concurrent.futures import ThreadPoolExecutor
from app.comptroller.service import get_supabase_config, postgrest_headers, _request_json, _paginated_get


def account_link(jurisdiction, taxpayer_id):
    empty = {'matched_record_id':None,'matched_account_number':None,'matched_owner_name':None}
    if not taxpayer_id or not jurisdiction.has_capability('real_property_linkage'):
        return empty
    url,key=get_supabase_config()
    locations=_request_json('GET',f'{url}/rest/v1/comptroller_permit_locations',postgrest_headers(key),params={
        'select':'id,address,zip,legal_name,location_name','county':f'eq.{jurisdiction.county_name}',
        'taxpayer_id':f'eq.{taxpayer_id}','order':'id.asc','limit':501})
    if len(locations)>500:
        return empty  # Do not choose a location from a truncated taxpayer portfolio.
    from app.comptroller.matching import match_imported_bpp_accounts
    matches={}
    uncertain=False
    for location in locations:
        match=match_imported_bpp_accounts(district_id=jurisdiction.district_id,
            address=location.get('address'),zip_code=location.get('zip'),
            legal_name=location.get('legal_name'),business_name=location.get('location_name'),
            permit_location_id=location['id'])
        if match and match.ambiguous:
            uncertain=True
        if match and match.confidence=='HIGH' and not match.ambiguous and match.candidate:
            matches[match.candidate.record_id]=match.candidate
    # Multiple locations resolving to the same account are fine. Different
    # accounts require review; never attach one account arbitrarily.
    if len(matches)!=1 or uncertain:
        return empty
    candidate=next(iter(matches.values()))
    return {'matched_record_id':candidate.record_id,'matched_account_number':candidate.account_number,
            'matched_owner_name':candidate.owner_name}


def refresh_mailing_account_links(jurisdiction):
    url,key=get_supabase_config();headers=postgrest_headers(key)
    rows=_paginated_get(url,headers,'bpp_intelligence_items',{
        'select':'id,source_taxpayer_id,matched_record_id,matched_account_number,matched_owner_name',
        'district_id':f'eq.{jurisdiction.district_id}','signal_type':'eq.mailing_address_change','order':'id.asc'},page_size=500)
    taxpayer_ids={r.get('source_taxpayer_id') for r in rows}
    with ThreadPoolExecutor(max_workers=4) as pool:
        links=dict(zip(taxpayer_ids,pool.map(lambda tid:account_link(jurisdiction,tid),taxpayer_ids)))
    for row in rows:
        link=links[row.get('source_taxpayer_id')]
        if any(row.get(k)!=v for k,v in link.items()):
            _request_json('PATCH',f'{url}/rest/v1/bpp_intelligence_items',headers,
                params={'id':f"eq.{row['id']}",'district_id':f'eq.{jurisdiction.district_id}'},json_payload=link)
    return {'evaluated':len(rows),'matched':sum(bool(links[r.get('source_taxpayer_id')]['matched_record_id']) for r in rows)}
