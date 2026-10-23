import json
import gzip
from pathlib import Path
import pytest
from app.commercial.formulas import Engine,FormulaError,parse
from app.commercial.store import Store,Conflict


def test_formula_recalculation_uses_inputs_and_lazy_if():
    engine=Engine({'Model':{'A1':{'v':100},'B1':{'v':.1},'C1':{'v':1000,'f':'IF(B1=0,1/0,A1/B1)'},'D1':{'v':1010,'f':'ROUND(C1+10,0)'}}},{('Model','B1'):.2})
    assert engine.cell('Model','D1')==510
    assert Engine({'S':{'A1':{'v':-2},'B1':{'v':4,'f':'-A1^2'}}},{('S','A1'):-3}).cell('S','B1')==9


def test_lookup_ignores_errors_outside_the_matched_return_cell():
    cells={'A1':{'v':'R1'},'B1':{'v':100},'C1':{'v':'#REF!','error':True},'A2':{'v':'R2'},'B2':{'v':200},'D1':{'v':100,'f':'VLOOKUP(A1,A1:C2,2,FALSE)'},'E1':{'v':5,'f':'IFERROR(B1/0,5)'}}
    e=Engine({'S':cells},{('S','B1'):150})
    assert e.cell('S','D1')==150
    assert e.cell('S','E1')==5


def test_iferror_does_not_hide_unvalidated_calculations():
    e=Engine({'S':{'A1':{'v':1},'B1':{'v':0,'f':'IFERROR(UNSUPPORTED(A1),0)'}}},{('S','A1'):2})
    with pytest.raises(FormulaError,match='needs validation'):e.cell('S','B1')


def test_unavailable_reference_does_not_turn_into_zero():
    e=Engine({'S':{'A1':{'v':2},'B1':{'v':9,'f':"A1+'[1]Missing'!A1"}}},{('S','A1'):3})
    with pytest.raises(FormulaError):e.cell('S','B1')


@pytest.fixture
def store(tmp_path):
    sheet={'A1':{'v':'Account'},'B1':{'v':'Category'},'C1':{'v':'Cap Rate'},'D1':{'v':'Net operating income'},'E1':{'v':'Value'},'F1':{'v':'Area'}}
    records=[]
    for row,account in [(2,'R1'),(3,'R2')]:
        sheet.update({f'A{row}':{'v':account},f'B{row}':{'v':'Office A'},f'C{row}':{'v':.1},f'D{row}':{'v':100},f'E{row}':{'v':1000,'f':f'D{row}/C{row}'},f'F{row}':{'v':200}})
        records.append({'id':account,'model':'office','account':account,'source_sheet':'Office','source_row':row,'source_file':'office.xlsx','source_hash':'abc'})
    model={'model':'office','sheet':'Office','fields':{'type':'office','account':'A','category':'B','cap_rate':'C','noi':'D','total_value':'E','area_sq_ft':'F'}}
    (tmp_path/'manifest.json').write_text(json.dumps({'models':[model],'records':records,'graphs':{'office':'office.json.gz'}}))
    with gzip.open(tmp_path/'office.json.gz','wt') as f:json.dump({'Office':sheet},f)
    return Store(tmp_path)


def preview(store,**kwargs):
    return store.preview({'filters':{'model':'office','category':'Office A'},'field':'cap_rate','value':.2,**kwargs})


def test_preview_apply_undo_and_stale_token(store):
    p=preview(store);assert p['count']==2
    assert p['items'][0]['after_value']==500
    assert store.detail('R1')['values']['total_value']==1000
    batch=store.commit(p['token'],'appraiser','Review cap rates')
    assert store.detail('R1')['values']['total_value']==500
    with pytest.raises(Conflict):store.commit(p['token'],'appraiser','Repeated click')
    store.undo(batch['batch_id'],'appraiser')
    assert store.detail('R1')['values']['total_value']==1000
    assert store.catalog({'ids':['R1']})['items'][0]['values']['total_value']==1000
    assert store.catalog({'ids':['R1']})['items'][0]['values']['cap_rate']==.1


def test_keep_overrides_and_zero_values(store):
    p=preview(store,filters={'ids':['R1']});store.commit(p['token'],'appraiser','Property-specific rate')
    p=preview(store,value=.25)
    assert p['count']==1 and p['skipped_overrides']==1
    p=preview(store,field='area_sq_ft',value=0)
    store.commit(p['token'],'appraiser','Zero area is deliberate')
    assert store.detail('R2')['values']['area_sq_ft']==0


def test_revision_invalidates_preview_after_new_property(store):
    p=preview(store)
    created=store.create('R1',{'account':'R3','category':'Office A'},'appraiser')
    assert created['values']['account']=='R3'
    assert created['values']['total_value']==1000
    with pytest.raises(Conflict):store.commit(p['token'],'appraiser','Old preview')
    p=preview(store,value=.25,include_overrides=True)
    store.commit(p['token'],'appraiser','Category update includes new property')
    assert store.detail(created['id'])['values']['total_value']==400


def test_reject_invalid_cap_and_duplicate_model_account(store):
    with pytest.raises(ValueError):preview(store,value=0)
    with pytest.raises(Conflict):store.create('R1',{'account':'R2'},'appraiser')


def test_empty_selection_never_means_entire_model(store):
    with pytest.raises(ValueError):preview(store,filters={'ids':[]})


def test_preview_does_not_persist_authentication_token(store):
    preview(store,access_token='do-not-store-this-token')
    with store.connect() as db:
        assert 'do-not-store-this-token' not in db.execute('SELECT payload FROM previews').fetchone()[0]


def test_create_is_audited_and_can_be_undone(store):
    created=store.create('R1',{'account':'R3'},'appraiser')
    history=store.history()
    assert history[0]['reason']=='Add R3 to Office'
    store.undo(history[0]['id'],'appraiser')
    with pytest.raises(KeyError):store.detail(created['id'])


def test_new_property_inherits_effective_template_inputs(store):
    p=preview(store,filters={'ids':['R1']});store.commit(p['token'],'appraiser','Specific cap')
    created=store.create('R1',{'account':'R3'},'appraiser')
    assert created['values']['cap_rate']==.2
    assert created['values']['total_value']==500


def test_api_requires_district_context_for_every_data_request(store,monkeypatch):
    from types import SimpleNamespace
    from fastapi import FastAPI,HTTPException
    from fastapi.testclient import TestClient
    from app.commercial.api import commercial_router
    monkeypatch.setenv('COMMERCIAL_DATA_DIR',str(store.directory.parent))
    calls=[]
    def resolve(token):
        calls.append(token)
        if token!='valid':raise HTTPException(401,'Sign in required')
        return SimpleNamespace(district_id=store.directory.name,email='appraiser')
    app=FastAPI();app.include_router(commercial_router(resolve));client=TestClient(app)
    assert client.post('/api/commercial/catalog',json={}).status_code==401
    response=client.post('/api/commercial/catalog',json={'access_token':'valid','district_id':'another-district'})
    assert response.status_code==200
    assert response.json()['accounts']==2
    assert client.post('/api/commercial/property',json={'access_token':'valid','id':'missing'}).status_code==404
    assert client.post('/api/commercial/equity',json={'id':'R1'}).status_code==401
    assert client.post('/api/commercial/equity',json={'access_token':'valid','id':'R1'}).json()['comparables'][0]['account']=='R2'
    assert calls==['','valid','valid','','valid']


def test_automatic_equity_excludes_subject_and_updates_with_saved_values(store):
    from app.commercial.equity import generate_equity
    e=generate_equity(store,'R1')
    assert [r['account'] for r in e['comparables']]==['R2']
    assert e['median_unit_value']==5
    assert e['status']=='limited'
    p=preview(store,filters={'ids':['R2']});store.commit(p['token'],'appraiser','Revised rate')
    assert generate_equity(store,'R1')['median_unit_value']==2.5


def test_automatic_equity_rejects_invalid_size_and_other_categories(store):
    from app.commercial.equity import generate_equity
    store.create('R1',{'account':'R3','category':'Different use'},'appraiser')
    p=preview(store,filters={'ids':['R2']},field='area_sq_ft',value=0)
    store.commit(p['token'],'appraiser','Zero area')
    assert generate_equity(store,'R1')['comparables']==[]
    assert generate_equity(store,'R2')['subject']['unit_value'] is None


def test_automatic_equity_deduplicates_accounts_and_ranks_without_values(store):
    from app.commercial.equity import generate_equity
    store.create('R1',{'account':'R3'},'appraiser')
    p=preview(store,filters={'ids':['R2']},field='cap_rate',value=.01)
    store.commit(p['token'],'appraiser','Higher value does not affect ranking')
    e=generate_equity(store,'R1')
    assert [r['account'] for r in e['comparables']]==['R2','R3']
    assert e['comparables'][0]['comparison_value']==10000


def test_packets_always_include_automatic_equity(store):
    from app.commercial.packets import build_packet
    pdf=build_packet(store,['R1'])
    assert pdf.startswith(b'%PDF')


@pytest.mark.parametrize('model,basis',[('apartments','units'),('hotels','rooms'),('office','area_sq_ft')])
def test_equity_uses_model_basis_and_one_row_per_account(model,basis):
    from app.commercial.equity import generate_equity
    def row(id,account,size,category='A',classification='A'):
        return {'id':id,'account':account,'model':model,'source_row':2,'values':{'category':category,'classification':classification,basis:size,'total_value':1000},'errors':{}}
    records=[row('s','R1',100),row('subject-component','R1',100),row('a','R2',100),row('duplicate','R2',110),row('wrong-size','R3',301),row('wrong-class','R4',100,classification='B')]
    class FakeStore:
        def detail(self,id):return next(r for r in records if r['id']==id)
        def records(self):return records
        def snapshot(self,r):return r
    e=generate_equity(FakeStore(),'s')
    assert e['basis']==basis
    assert [r['id'] for r in e['comparables']]==['a']
    assert e['median_unit_value']==10


def test_equity_details_preserve_rates_zero_missing_and_errors():
    from app.commercial.equity import comparison_sections
    records=[{'values':{'cap_rate':.0625,'vacancy_rate':0,'expense_rate':.35,'noi':1234.56,'primary_rent':21,'projected_gross_rent':None},'errors':{'projected_gross_rent':'#REF!'}}, {'values':{'cap_rate':.07,'vacancy_rate':None,'expense_rate':.4,'noi':1000},'errors':{}}]
    sections=comparison_sections(records)
    rows={r['key']:r for s in sections for r in s['rows']}
    assert rows['cap_rate']['format']=='rate'
    assert rows['expense_rate']['cells'][1]['value']==.4
    assert rows['vacancy_rate']['cells'][0]['value']==0
    assert rows['vacancy_rate']['cells'][1]['value'] is None
    assert rows['noi']['format']=='money'
    assert rows['primary_rent']['label'].endswith('(source basis)')
    assert not rows['projected_gross_rent']['label'].endswith('(source basis)')
    assert rows['projected_gross_rent']['cells'][0]['error']=='#REF!'
