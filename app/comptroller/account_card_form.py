"""Editable field worksheet on the existing BPP intelligence item."""
from datetime import date,datetime
from decimal import Decimal,InvalidOperation
from zoneinfo import ZoneInfo
from .new_account_enrichment import NewAccountEnrichmentError

SECTIONS=[
 ('Account',[('property_id','Property ID','text'),('appraiser','Appraiser / initials','text'),('tax_year','Tax year','number')]),
 ('Mailing',[('mailing_name','Mailing name','text'),('mailing_address','Mailing address','text'),('mailing_city','Mailing city','text'),('mailing_state','Mailing state','text'),('mailing_zip','Mailing ZIP','text')]),
 ('Business location',[('business_name','Location / DBA name','text'),('situs_address','Situs address','text'),('situs_city','Situs city','text'),('situs_state','Situs state','text'),('situs_zip','Situs ZIP','text')]),
 ('Contacts',[('owner_contact','Owner contact','text'),('contact_name','Contact name','text'),('contact_phone','Contact phone','tel'),('contact_email','Contact email','email')]),
 ('Property and codes',[('map_id','Map #','text'),('neighborhood','Neighborhood','text'),('legal_description','Legal description','text'),('property_link','Property link','text'),('link_reason','Link reason','text'),('tug','TUG','text'),('appraiser_code','Appraiser code','text'),('stpb_code','STPB code','text'),('last_inspected','Last inspected','date'),('section','Section','text'),('sic','SIC','text')]),
 ('Permit and valuation',[('sales_tax_id','Sales tax ID','text'),('permit_id','Permit ID','text'),('permit_issued','Permit issued','date'),('additional_permit_date','Additional permit date','date'),('new_value','New value (Y / N)','text'),('new_value_year','New valuation year','number'),('comments','Comments','textarea')]),
]
FIELDS={name:kind for _,fields in SECTIONS for name,_,kind in fields}
SEGMENT_FIELDS=('valuation_method','type_code','flat_value','appraisal_method')

def eligible(item):
    return item.source_table=='bpp_intelligence_items' and (item.signal_type=='new_business' or
        (item.signal_type=='county_clerk_dba' and (item.raw.get('filing_type')=='ASSUMED NAME' or item.resolution=='NEW_ACCOUNT_NEEDED')))

def validate(data):
    if not isinstance(data,dict) or set(data)-set(FIELDS)-{'segments'}:raise NewAccountEnrichmentError('Unknown account-card fields.')
    clean={}
    for key,kind in FIELDS.items():
        value=data.get(key,'')
        if not isinstance(value,str) or len(value)>(4000 if key=='comments' else 500):raise NewAccountEnrichmentError('Invalid '+key.replace('_',' ')+'.')
        value=value.strip()
        if value and kind=='date':
            try:date.fromisoformat(value)
            except ValueError:raise NewAccountEnrichmentError('Enter a valid '+key.replace('_',' ')+'.')
        if value and kind=='number' and (not value.isdigit() or not 1900<=int(value)<=2200):raise NewAccountEnrichmentError('Enter a four-digit year.')
        clean[key]=value
    rows=data.get('segments',[])
    if not isinstance(rows,list) or len(rows)>20:raise NewAccountEnrichmentError('Use at most 20 valuation segments.')
    clean['segments']=[]
    for row in rows:
        if not isinstance(row,dict) or set(row)-set(SEGMENT_FIELDS):raise NewAccountEnrichmentError('Invalid valuation segment.')
        segment={}
        for key in SEGMENT_FIELDS:
            value=row.get(key,'')
            if not isinstance(value,str) or len(value)>80:raise NewAccountEnrichmentError('Invalid segment value.')
            segment[key]=value.strip()
        if segment['flat_value']:
            try:
                amount=Decimal(segment['flat_value'])
                if not amount.is_finite() or abs(amount)>Decimal('1000000000000') or amount!=amount.quantize(Decimal('.01')):raise InvalidOperation
            except InvalidOperation:raise NewAccountEnrichmentError('Enter a segment value with at most two decimal places.')
        clean['segments'].append(segment)
    return clean

def total(data):
    amounts=[Decimal(r['flat_value']) for r in data.get('segments',[]) if r.get('flat_value','')!='']
    return str(sum(amounts,Decimal('0')).quantize(Decimal('.01'))) if amounts else None

def form(item,jurisdiction,summary):
    if not eligible(item):raise NewAccountEnrichmentError('A new-account card is available for new-business and assumed-name leads.')
    data={name:'' for name in FIELDS}
    filing=(item.raw.get('evidence') or {}).get('filing') or {}
    mailing=filing.get('mailing_address') or {}
    year=str(datetime.now(ZoneInfo(jurisdiction.timezone)).year)
    data.update(property_id=item.matched_account_number or '',appraiser_code=summary['appraiser'].get('appraiser') or '',tax_year=year,
        mailing_name=item.legal_name or '',mailing_address=' '.join(filter(None,[mailing.get('line1'),mailing.get('line2')])),
        mailing_city=mailing.get('city') or '',mailing_state=mailing.get('state') or '',mailing_zip=mailing.get('zip') or '',
        business_name=item.business_name or '',situs_address=item.source_address or '',situs_city=item.source_city or '',situs_state=item.source_state or '',situs_zip=item.source_zip or '',
        map_id=summary.get('map_id') or '',neighborhood=summary.get('neighborhood') or '',property_link=summary.get('suggested_property_link') or '',tug=str(summary.get('tug') or ''),
        sales_tax_id=item.raw.get('source_taxpayer_id') or '',permit_issued=item.permit_start_date or '',new_value_year=year)
    data['segments']=[{key:'' for key in SEGMENT_FIELDS} for _ in range(4)]
    saved=item.raw.get('account_card_draft')
    if saved:data.update({k:v for k,v in validate(saved).items() if k in saved})
    return {'profile':'lcad' if jurisdiction.slug=='lubbock' else 'county','district_name':jurisdiction.name,
            'title':'Personal property field review worksheet','sections':[{'title':title,'fields':[{'name':n,'label':label,'type':kind} for n,label,kind in fields]} for title,fields in SECTIONS],
            'data':data,'total_value':total(data),'version':item.raw.get('account_card_version',0),
            'saved_at':item.raw.get('account_card_saved_at'),'saved_by':item.raw.get('account_card_saved_by'),
            'existing_accounts':summary.get('personal_property_accounts',[]),'checks':summary.get('exceptions',[])}
