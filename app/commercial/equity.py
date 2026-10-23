"""Reproducible, characteristic-based model comparisons; never rank by value."""
import math
from statistics import median

BASES={'apartments':('units','unit'),'hotels':('rooms','room'),'senior_housing':('units','unit / bed')}

def positive(v):
    return isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) and v>0

def generate_equity(store,record_id,limit=6):
    subject=store.detail(record_id)
    basis,unit=BASES.get(subject['model'],('area_sq_ft','sq ft'))
    def normalize(r):
        v=r['values']; size=v.get(basis)
        key='total_value' if v.get('total_value') is not None else 'indicated_value'
        value=v.get(key)
        valid=positive(size) and positive(value) and not any(r.get('errors',{}).get(k) for k in (basis,key))
        return {**r,'size':size,'comparison_value':value,'unit_value':value/size if valid else None}
    s=normalize(subject); candidates={};v=s['values']
    category=str(v.get('category') or '').strip().casefold()
    # Exact source use category is mandatory: sparse groups are disclosed, not silently broadened.
    if category and s['unit_value'] is not None:
        for raw in store.records():
            if raw['model']!=s['model'] or raw.get('reference_only'):continue
            r=normalize(store.snapshot(raw));cv=r['values']
            if r['account'].strip().upper()==s['account'].strip().upper():continue
            if str(cv.get('category') or '').strip().casefold()!=category or r['unit_value'] is None:continue
            if any(v.get(k) and cv.get(k)!=v[k] for k in ('classification','quality','service_type')):continue
            ratio=r['size']/s['size']
            if not .5<=ratio<=2:continue
            yeargap=abs(cv['year_built']-v['year_built']) if positive(cv.get('year_built')) and positive(v.get('year_built')) else None
            r['match_notes']=['Same model and category',f"Size {ratio:.2f}× subject"]+([f'Built {yeargap:g} years apart'] if yeargap is not None else ['Year comparison unavailable'])
            rank=(abs(math.log(ratio)),yeargap if yeargap is not None else 9999,r['account'],r['source_row'])
            account=r['account'].strip().upper()
            if account not in candidates or rank<candidates[account][0]:candidates[account]=(rank,r)
    selected=[r for _,r in sorted(candidates.values(),key=lambda pair:pair[0])[:limit]]
    mid=median([r['unit_value'] for r in selected]) if selected else None
    return {'subject':s,'comparables':selected,'basis':basis,'unit':unit,'eligible_count':len(candidates),'median_unit_value':mid,'unadjusted_indication':mid*s['size'] if mid is not None else None,'status':'ready' if len(selected)>=3 else 'limited','rules':'Same model and exact category; matching classification, quality and hotel service type when supplied by the subject; size between 50% and 200% of subject. Ranked by proportional size difference, then year built difference. One row per account; subject account excluded. Values do not affect ranking.','explanation':'Unadjusted model comparisons, not sales. Median uses the displayed comparables. No location, condition or other dollar adjustments have been inferred.','missing_reason':('Subject needs a positive comparison value, positive '+unit+' and a source category.' if s['unit_value'] is None or not category else 'Fewer than three qualifying accounts; review the available evidence before hearing use.') if len(selected)<3 else None}
