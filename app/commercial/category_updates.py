"""Current category assumptions, using the same fields as the source models."""
from statistics import median
from collections import Counter
from .store import DISPLAY_LABELS, TEXT_FIELDS


def category_summary(store, filters):
    model=filters.get('model')
    if model not in store.definitions:raise ValueError('Choose a valuation model')
    with store.lock,store.connect() as db:
        rows=[store.snapshot(r) for r in store.records(db) if r['model']==model and not r.get('reference_only')]
        rows=[r for r in rows if store.match(r,filters)]
        fields=[]
        for key,column in store.definitions[model]['fields'].items():
            if key in TEXT_FIELDS or not store.editable(model,key):continue
            values=[r['values'].get(key) for r in rows]
            numbers=[v for v in values if isinstance(v,(int,float)) and not isinstance(v,bool)]
            group='Other model inputs'
            if key.endswith('_rate'):group='Rates & expenses'
            elif 'rent' in key or key.startswith('mix_'):group='Rent & unit schedule'
            elif 'area' in key or key in ('units','rooms'):group='Property dimensions'
            elif 'value' in key or 'land' in key or 'improvement' in key:group='Value components'
            label=DISPLAY_LABELS.get(key,key.replace('_',' ').title())
            unit='%' if key.endswith('_rate') else 'sq ft' if 'area' in key or key.endswith('_sq_ft') else 'count' if key in ('units','rooms') or key.endswith('_units') else 'source units'
            if 'rent' in key and not key.endswith('_rate'):
                unit='$ / unit / month' if 'monthly_rent' in key else '$ / sq ft / year' if model in ('retail','office','supermarkets','pharmacies') else '$ / sq ft / month' if model=='miniwarehouse_storage' else '$ / unit / month' if model=='senior_housing' else 'source rent basis'
            if key in ('year_built','effective_year'):unit='year'
            if key=='effective_age':unit='years'
            if group=='Value components' or key.startswith('room_revenue') or key=='other_income':unit='$'
            if key.startswith('mix_'):
                prefix='_'.join(key.split('_')[:2]);quantity=store.definitions[model]['fields'][prefix+'_units']
                cells=store.graph(model)[store.definitions[model]['sheet']]
                bed=str(cells.get(quantity+'1',{}).get('v') or 'Units').replace('# of ','')
                suffix={'units':'Units','monthly_rent':'Monthly rent','unit_area_sq_ft':'Unit area (sq ft)'}[key[len(prefix)+1:]]
                label=f'{bed} · configuration {int(key.split("_")[1])} · {suffix}'
            fields.append(dict(key=key,column=column,label=label,group=group,unit=unit,minimum=min(numbers) if numbers else None,maximum=max(numbers) if numbers else None,median=median(numbers) if numbers else None,missing=len(values)-len(numbers),overrides=sum(key in r['override_fields'] for r in rows)))
        categories=[]
        for name,count in sorted(Counter(str(r['values'].get('category') or '') for r in rows).items()):
            members=[r for r in rows if str(r['values'].get('category') or '')==name]
            rates=Counter(r['values'].get('cap_rate') for r in members if isinstance(r['values'].get('cap_rate'),(int,float)))
            categories.append(dict(name=name,count=count,cap_rates=[{'value':value,'count':n} for value,n in sorted(rates.items())],missing_cap_rates=count-sum(rates.values())))
        return dict(model=model,categories=categories,count=len(rows),accounts=len({r['account'] for r in rows}),fields=fields,revision=db.execute('SELECT revision FROM state').fetchone()[0],sheet=store.definitions[model]['sheet'])
