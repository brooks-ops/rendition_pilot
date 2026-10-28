"""Offline parsing of captured public records. No source networking."""
import hashlib,re
from datetime import datetime
from urllib.parse import urlparse
from bs4 import BeautifulSoup
HOST='erecord.lubbockcounty.gov'
def clean(s):return re.sub(r'\s+',' ',s or '').strip() or None
def field_value(group,label):
    if group is None:return None
    el=next((x for x in group.select('span.fieldLabel') if clean(x.get_text())==label),None)
    return el.find_next_sibling('span',class_='field') if el else None
def value(group,label):
    el=field_value(group,label);return clean(el.get_text(' ',strip=True)) if el else None
def address(group,label):
    block=field_value(group,label)
    if block is None:return None
    fields={k:value(block,v) for k,v in [('line1','Address1'),('line2','Address2'),('city','City'),('state','State'),('zip','Zip')]}
    return fields if any(fields.values()) else None

def parse_detail(capture,index_record):
    url=urlparse(capture.get('url',''))
    if url.scheme!='https' or url.hostname!=HOST or url.path!='/recorder/eagleweb/viewDoc.jsp':raise ValueError('Unrecognized source record URL')
    kind,number=index_record['description'].strip().rsplit('\n',1)
    if kind not in ('ASSUMED NAME','ASSUMED NAME ABANDONMENT') or not re.fullmatch(r'\d{10}',number):raise ValueError('Unrecognized filing identity')
    root=BeautifulSoup(capture.get('html') or '','html.parser').find(id='presentation')
    if root is None or value(root,'Document Number')!=number:raise ValueError('Detail/summary identity mismatch')
    if not re.search(re.escape(kind)+r'\s*-\s*'+re.escape(number),capture.get('body','')):raise ValueError('Detail filing type does not match index')
    groups={clean(f.legend.get_text()):f for f in root.find_all('fieldset') if f.legend}
    business=groups.get('AssumedName');owner_group=groups.get('Owner(s)');owners=[]
    if owner_group:
        for table in owner_group.find_all('table'):
            if table.find('table'):continue
            rows=table.find_all('tr');headers=[clean(c.get_text()) for c in rows[0].find_all(['td','th'],recursive=False)] if rows else []
            name_index=next((i for i,h in enumerate(headers) if h in ('Name(s)','Name','Owner Name')),None)
            if name_index is None:continue
            for row in rows[1:]:
                cells=row.find_all(['td','th'],recursive=False)
                if len(cells)>name_index:
                    owner=clean(cells[name_index].get_text(' ',strip=True))
                    if owner and owner not in owners:owners.append(owner)
    recorded_raw=value(root,'Filing Date')
    try:recorded=datetime.strptime(recorded_raw,'%m/%d/%Y %I:%M:%S %p')
    except (TypeError,ValueError) as exc:raise ValueError('Missing or invalid filing timestamp') from exc
    expiry_raw=value(root,'Expiration Date')
    expiry=datetime.strptime(expiry_raw,'%m/%d/%Y').date().isoformat() if expiry_raw else None
    business_address=address(business,'Business Address');mailing_address=address(business,'Mailing Address') or address(root,'Mailing Address')
    assumed=value(business,'Name')
    if not assumed:raise ValueError('Assumed name missing')
    return {'state':'TX','county':'LUBBOCK','document_number':number,'filing_type':kind,'recording_date':recorded.date().isoformat(),'recording_timestamp_local':recorded.isoformat(),'recording_timezone':'America/Chicago','recording_date_raw':recorded_raw,'assumed_name':assumed,'owners':owners,'owner_name':owners[0] if len(owners)==1 else None,'owner_name_complete':bool(owners),'business_address':business_address,'mailing_address':mailing_address,'business_conducted_as':value(business,'Business Conducted As'),'expiration_date':expiry,'additional_groups':{k:clean(v.get_text(' ',strip=True)) for k,v in groups.items() if k not in ('AssumedName','Owner(s)')},'source_url':capture['url'],'source_sha256':hashlib.sha256(capture['html'].encode()).hexdigest(),'missing_fields':[k for k,v in [('owners',owners),('business_address',business_address),('mailing_address',mailing_address),('expiration_date',expiry)] if not v],'images_available_online':False if "Images are available at the County Clerk's office" in capture.get('body','') else None}

