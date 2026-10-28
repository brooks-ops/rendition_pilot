"""Lubbock office Apex area table supplied by the district, October 2026."""
import json
import re
from functools import lru_cache
from pathlib import Path

@lru_cache(maxsize=1)
def catalog():
    return json.loads(Path(__file__).with_name('orion_area_codes.json').read_text())

def normalized(value):
    return re.sub(r'[^a-z0-9]+',' ',str(value).casefold()).strip()

def defaults(record):
    entries=catalog();by_code={e['code']:e for e in entries}
    values=record.get('values',{})
    matches=set()
    for field in ('classification','category'):
        text=str(values.get(field) or '').strip()
        if text in by_code:matches.add(text)
        for e in entries:
            if normalized(text) and normalized(text) in (normalized(e['name']),normalized(e['description'])):matches.add(e['code'])
    # Only models with an unambiguous occupancy default; hotel/senior variants
    # and construction-specific canopies/loading sections require a selection.
    code=next(iter(matches)) if len(matches)==1 else None
    if not matches:code={'apartments':'300','office':'344','pharmacies':'511','supermarkets':'446','miniwarehouse_storage':'386'}.get(record.get('model'))
    if not code:return {}
    e=by_code[code]
    return {kind:{'area_code':code,'area_description':e['description']} for kind in ('main','addition')}
