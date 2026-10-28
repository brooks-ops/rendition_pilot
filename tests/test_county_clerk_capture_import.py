import json
import pytest
from app.county_clerk.import_captured import prepare
from tests.test_lubbock_dba_prototype import capture


def write_set(tmp_path, *, gap=False, changed_total=False, duplicate=False):
    entries=[]
    for i in range(2):
        number=f'202670000{i+1}'
        c,e=capture(number=number)
        (tmp_path/(number+'.json')).write_text(json.dumps(c));entries.append(e)
    if duplicate:entries[1]=entries[0]
    for i,e in enumerate(entries,1):
        first=i+1 if gap and i==2 else i
        total=3 if changed_total and i==2 else 2
        (tmp_path/f'page-{i}.json').write_text(json.dumps({'text':f'{total} items found, displaying {first} to {first}.','records':[e]}))
    (tmp_path/'index.json').write_text(json.dumps({'pages':2,'records':entries}))


def test_paginated_set_preserves_all_original_pages(tmp_path):
    write_set(tmp_path)
    records,original=prepare(tmp_path,{'county':'LUBBOCK','state':'TX'},'2026-01-01','2026-10-31')
    assert len(records)==2 and len(json.loads(original)['pages'])==2
    assert records[0]['owner_name']=='OWNER ONE; OWNER TWO'
    assert records[0]['business_address']['line1']=='100 MAIN ST'

@pytest.mark.parametrize('change',['gap','changed_total','duplicate'])
def test_partial_changing_or_duplicate_results_are_rejected(tmp_path,change):
    write_set(tmp_path,**{change:True})
    with pytest.raises(ValueError):prepare(tmp_path,{'county':'LUBBOCK','state':'TX'},'2026-01-01','2026-10-31')


def test_precomputed_matches_reject_a_changed_bpp_export(monkeypatch):
    from types import SimpleNamespace
    from app.county_clerk import import_captured, service
    from app.comptroller import cad_review
    monkeypatch.setattr(cad_review,'latest_completed_import',lambda j:{'id':'new-export'})
    monkeypatch.setattr(service,'request',lambda *a,**kw:pytest.fail('Must not write stale classifications'))
    with pytest.raises(ValueError,match='export changed'):
        import_captured.commit_prepared({'district_id':'d'},SimpleNamespace(id='j'),[],b'{}','2026-01-01','2026-09-30',cad_import_id='old-export')
