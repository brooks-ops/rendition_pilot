"""Reuse archived certificate details only when their current index is unchanged.

The original capture timestamp is retained. New/changed summaries always fetch
full details. Cached account matches are never used for new filings.
"""
import hashlib,json,re
from . import service
from .captured import parse_detail

def same_summary(left,right):
    norm=lambda text:re.sub(r'\s+',' ',text or '').strip()
    return (left.get('description')==right.get('description') and left.get('url')==right.get('url')
            and norm(left.get('summary'))==norm(right.get('summary')))

def load(source,start,end):
    rows=service.request('GET','county_clerk_dba_imports',params={
        'select':'id,sha256,metadata','source_id':'eq.'+source['id'],'district_id':'eq.'+source['district_id'],
        'status':'eq.COMPLETED','coverage':'eq.COMPLETE','search_start':'lte.'+end,'search_end':'gte.'+start,
        'order':'extracted_count.desc,created_at.desc','limit':1})
    if not rows or rows[0].get('metadata',{}).get('storage_bucket')!='county-clerk-records':return {}
    original,_=service.original(source['district_id'],rows[0]['id'])
    if hashlib.sha256(original).hexdigest()!=rows[0]['sha256']:raise ValueError('Archived source integrity failure')
    archive=json.loads(original);entries=archive['index']['records'];details=archive['details']
    if len(entries)!=len(details):raise ValueError('Incomplete archived source details')
    cached={}
    for entry,capture in zip(entries,details):
        record=parse_detail(capture,entry)
        if start<=record['recording_date']<=end:cached[entry['description']]={'entry':entry,'capture':capture}
    return cached
