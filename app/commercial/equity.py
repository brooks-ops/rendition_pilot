"""Reproducible, characteristic-based model comparisons; never rank by value."""
import math
from statistics import median

BASES={'apartments':('units','unit'),'hotels':('rooms','room'),'senior_housing':('units','unit / bed')}

def positive(v):
    return isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) and v>0

def equity_pool(store):
    revision,rows,_=store.read_view()
    cached=getattr(store,'_equity_pool',None)
    if cached and cached[0]==revision:return cached[1]
    pools={}
    for row in rows:
        if not row.get('reference_only'):pools.setdefault(row['model'],[]).append(row)
    store._equity_pool=(revision,pools)
    return pools


def generate_equity(store,record_id,limit=6):
    subject=store.detail(record_id)
    return select_equity(subject,equity_pool(store).get(subject['model'],[]),limit)


def select_equity(subject,pool,limit=6):
    """Always populate available model peers; disclose broader matches and missing subject inputs."""
    import re
    basis,unit=BASES.get(subject['model'],('area_sq_ft','sq ft'))
    def normalize(r):
        v=r['values'];size=v.get(basis)
        if basis=='area_sq_ft' and not positive(size):size=v.get('leasable_area_sq_ft')
        key='total_value' if v.get('total_value') is not None else 'indicated_value'
        value=v.get(key)
        valid=positive(size) and positive(value) and not any(r.get('errors',{}).get(k) for k in (basis,key))
        return {**r,'size':size,'comparison_value':value,'unit_value':value/size if valid else None}
    def text(value):return str(value or '').strip().casefold()
    def tokens(value):return set(re.findall(r'[a-z]+',text(value)))-{'a','b','c','good','fair','poor','above','below','average'}
    s=normalize(subject);v=s['values'];category=text(v.get('category'));candidates={}
    for raw in pool:
        if raw['model']!=s['model'] or raw.get('reference_only'):continue
        if text(raw['account'])==text(s['account']):continue
        r=normalize(raw);cv=r['values']
        if r['unit_value'] is None:continue
        exact=bool(category) and text(cv.get('category'))==category
        characteristics=all(not v.get(k) or text(cv.get(k))==text(v[k]) for k in ('classification','quality','service_type'))
        use_match=bool(v.get('classification')) and text(cv.get('classification'))==text(v['classification'])
        a=tokens(category);b=tokens(cv.get('category'));overlap=len(a&b)/len(a|b) if a|b else 0
        ratio=r['size']/s['size'] if positive(s['size']) else None
        size_close=ratio is not None and .5<=ratio<=2
        tier=0 if exact and characteristics and size_close else 1 if exact else 2 if use_match or overlap>=.5 else 3
        yeargap=abs(cv['year_built']-v['year_built']) if positive(cv.get('year_built')) and positive(v.get('year_built')) else None
        descriptions=['Exact category and characteristics','Same category, broader size or characteristics','Related use / category','Broader model peer for review']
        r['match_tier']=tier;r['match_label']=descriptions[tier]
        r['match_notes']=[descriptions[tier],f"Size {ratio:.2f}× subject" if ratio is not None else 'Subject size unavailable; ranked using available characteristics',f'Built {yeargap:g} years apart' if yeargap is not None else 'Year comparison unavailable']
        rank=(tier,not characteristics,-overlap,abs(math.log(ratio)) if ratio is not None else 0,yeargap if yeargap is not None else 9999,r['account'],r['source_row'])
        account=text(r['account'])
        if account not in candidates or rank<candidates[account][0]:candidates[account]=(rank,r)
    selected=[r for _,r in sorted(candidates.values(),key=lambda pair:pair[0])[:limit]]
    mid=median([r['unit_value'] for r in selected]) if selected else None
    broader=any(r['match_tier']>0 for r in selected)
    reason=[]
    if broader:reason.append('Search automatically expanded beyond exact category / size matches. Broader peers are identified for appraiser review.')
    if s['unit_value'] is None:reason.append('Subject size or value is incomplete; peer comparisons are available, but the subject unit value cannot be calculated.')
    if not selected:reason.append('No other accounts with usable size and value exist in this model.')
    return {'sections':comparison_sections([s]+selected),'subject':s,'comparables':selected,'basis':basis,'unit':unit,'eligible_count':len(candidates),'median_unit_value':mid,'unadjusted_indication':mid*s['size'] if mid is not None and positive(s['size']) else None,'status':'review' if broader or reason else 'ready','rules':'Automatically fills up to six distinct accounts: exact category / characteristics / size first, then same category with wider size or characteristics, then related use, then closest available broader model peers. Ranking uses property characteristics, category similarity, proportional size and year built; never value. Subject account and duplicate account components are excluded.','explanation':'Unadjusted model peer comparisons, not sales. Median uses all displayed peers, including any broader matches identified for review. No location, condition or dollar adjustments have been inferred.','missing_reason':' '.join(reason) or None}


# The same source-backed comparison grid drives the browser and packet PDF.
DETAIL_GROUPS = [
    ('Property characteristics', ['category','classification','quality','service_type','year_built','effective_year','effective_age','area_sq_ft','leasable_area_sq_ft','units','rooms']),
    ('Operating assumptions', ['cap_rate','loaded_cap_rate','tax_rate','vacancy_rate','expense_rate','operating_expense_rate','management_rate','reserve_rate','allowable_expense_rate','departmental_expense_rate','franchise_rate','insurance_rate','other_income_rate','bad_debt_rate','loss_to_lease_rate']),
    ('Rent and space components', ['rent','rent_per_unit','primary_area_sq_ft','primary_rent','secondary_area_sq_ft','secondary_rent','office_area_sq_ft','office_rent','tertiary_area_sq_ft','tertiary_rent','quaternary_area_sq_ft','quaternary_rent']),
    ('Annual income and expenses', ['room_revenue_2022','room_revenue_2025','room_revenue_2026','projected_gross_rent','vacancy_loss','effective_gross_rent','other_income','other_income_amount','bad_debt_amount','effective_gross_income','total_expenses','reserve_amount','noi','actual_noi']),
    ('Value reconciliation', ['land_value','improvement_value','indicated_value','actual_indicated_value','other_improvements','other_land','other_adjustments','land_adjustment','other_component_value','other_component_land_value','total_value']),
]


def comparison_sections(records):
    from .store import DISPLAY_LABELS
    sections=[]
    for title,keys in DETAIL_GROUPS:
        rows=[]
        for key in keys:
            if not any(key in r['values'] for r in records):continue
            kind='rate' if key.endswith('_rate') else 'number' if key.endswith('_sq_ft') or key in ('units','rooms','effective_age') else 'text' if key in ('category','classification','quality','service_type','year_built','effective_year') else 'money'
            label=DISPLAY_LABELS.get(key,key.replace('_sq_ft',' (sq ft)').replace('_',' ').title())
            if key in ('rent','rent_per_unit','primary_rent','secondary_rent','office_rent','tertiary_rent','quaternary_rent'):label+=' (source basis)'
            rows.append({'key':key,'label':label,'format':kind,'cells':[{'value':r['values'].get(key),'error':r.get('errors',{}).get(key)} for r in records]})
        if rows:sections.append({'title':title,'rows':rows})
    return sections
