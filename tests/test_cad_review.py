from dataclasses import asdict
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.comptroller import cad_review, jurisdictions, matching, property_adapter, property_import, service
from backend import main as backend
from tests.comptroller_fakes import FakeSupabase
from tests.test_backend_intelligence_queue import make_district, make_item
from tests.test_property_import import make_jurisdiction
from tests.test_property_matching import make_property

@pytest.fixture
def store(monkeypatch):
    fake = FakeSupabase()
    monkeypatch.setenv('SUPABASE_URL','https://fake.supabase.co')
    monkeypatch.setenv('SUPABASE_SERVICE_ROLE_KEY','fake')
    for module in (service,cad_review,jurisdictions,property_adapter,property_import):
        monkeypatch.setattr(module,'_request_json',fake.request_json)
    j=make_jurisdiction()
    fake.jurisdictions[j.id]=asdict(j)
    return fake,j


def seed(store, account='P1', owner=None, address='100 MAIN ST', **extra):
    fake,j=store
    prop=make_property(account,address,'79401',account,owner_name=owner,**extra)
    row=asdict(prop);row['id']=row.pop('property_id')
    row['situs_address_normalized']=cad_review.normalize_address(address).normalized
    fake.real_property_records[account]=row
    return prop


def item():
    return make_item(source_address='100 MAIN ST', business_name='ACME', legal_name='ACME LLC', raw={'source_permit_location_id':'permit-1'})


def match(store, **overrides):
    _,j=store
    args=dict(district_id=j.district_id,permit_location_id='permit-1',permit_address='100 MAIN ST',permit_zip='79401',permit_legal_name='ACME LLC',permit_location_name='ACME',candidates=[])
    args.update(overrides)
    return matching.match_closure_to_account(**args)


def decide(store, prop, decision):
    _,j=store
    return cad_review.save_decision(j,item(),property_record_id=prop.property_id,decision=decision,notes='Reviewed the source records.',reviewed_by='reviewer-1')


def test_human_confirmation_is_reused_without_names_and_after_reimport(store):
    fake,j=store;prop=seed(store)
    decide(store,prop,'CONFIRMED')
    fake.real_property_records['P1']['owner_name']='UPDATED BUSINESS NAME'
    result=match(store)
    assert result.confidence=='HIGH'
    assert result.candidate.account_number=='P1'
    assert result.signals['human_decision']=='CONFIRMED'
    assert len(fake.cad_match_decisions)==1


def test_rejected_account_cannot_return_through_rendition_fallback(store):
    prop=seed(store,owner='ACME')
    decide(store,prop,'REJECTED')
    result=match(store,candidates=[matching.MatchCandidate('rendition-1','P1','ACME',2026)])
    assert result.candidate is None
    assert result.confidence=='UNMATCHED'
    assert result.signals['rejected_accounts']=='P1'


def test_new_confirmation_replaces_previous_and_rejection_does_not_revive_it(store):
    one=seed(store,'P1');two=seed(store,'P2')
    decide(store,one,'CONFIRMED');decide(store,two,'CONFIRMED')
    assert match(store).candidate.account_number=='P2'
    decide(store,two,'REJECTED')
    confirmed,_=cad_review.current_decisions(list(store[0].cad_match_decisions.values()))
    assert confirmed is None


def test_dba_and_separate_suite_are_used_by_matching(store):
    seed(store,owner='HOLDING COMPANY',dba_name='ACME',situs_unit='200')
    assert match(store,permit_address='100 MAIN ST STE 200').confidence=='HIGH'
    assert match(store,permit_address='100 MAIN ST STE 100').candidate is None


def test_review_shows_conflicting_suites_but_never_real_property_as_bpp(store):
    seed(store,'P1',situs_unit='200');seed(store,'R1',owner='ACME')
    data=cad_review.candidate_review(store[1],item())
    assert [c['real_account_number'] for c in data['candidates']]==['P1']
    assert data['candidates'][0]['name_signal']=='MISSING'
    assert data['candidates'][0]['suite_signal']=='MISSING'


def test_confirm_address_conflict_requires_notes_and_land_cannot_be_selected(store):
    prop=seed(store,address='200 MAIN ST')
    with pytest.raises(cad_review.CadReviewError,match='Explain'):
        cad_review.save_decision(store[1],item(),property_record_id=prop.property_id,decision='CONFIRMED',notes='',reviewed_by='user')
    land=seed(store,'R1')
    with pytest.raises(cad_review.CadReviewError,match='personal-property'):
        decide(store,land,'CONFIRMED')


def test_property_from_other_jurisdiction_cannot_be_confirmed(store):
    prop=seed(store)
    store[0].real_property_records['P1']['jurisdiction_id']='other'
    with pytest.raises(cad_review.CadReviewError):decide(store,prop,'CONFIRMED')


@pytest.mark.parametrize('endpoint,payload',[
    (backend.intelligence_cad_candidates,backend.CadCandidatesRequest(access_token='x',source_table='bpp_intelligence_items',item_id='item')),
    (backend.intelligence_cad_decision,backend.CadDecisionRequest(access_token='x',source_table='bpp_intelligence_items',item_id='item',property_record_id='prop',decision='CONFIRMED')),
])
def test_new_review_endpoints_block_cross_district_items(monkeypatch,endpoint,payload):
    monkeypatch.setattr(backend,'require_bpp_access',lambda _:make_district())
    monkeypatch.setattr(backend.comptroller_intelligence,'get_intelligence_item',lambda *a:make_item(district_id='other'))
    with pytest.raises(HTTPException) as error:endpoint(payload)
    assert error.value.status_code==403


@pytest.mark.parametrize('endpoint,payload',[
    (backend.intelligence_cad_health,backend.ComptrollerStatusRequest(access_token='x')),
    (backend.intelligence_cad_import_preview,backend.CadImportRequest(access_token='x',filename='x.csv',file_base64='')),
    (backend.intelligence_cad_import,backend.CadImportRequest(access_token='x',filename='x.csv',file_base64='')),
])
def test_data_endpoints_require_bpp_access(monkeypatch,endpoint,payload):
    def deny(_):raise HTTPException(status_code=403,detail='BPP access required')
    monkeypatch.setattr(backend,'require_bpp_access',deny)
    with pytest.raises(HTTPException) as error:
        endpoint(payload, backend.BackgroundTasks()) if endpoint == backend.intelligence_cad_import else endpoint(payload)
    assert error.value.status_code==403


def test_import_quality_and_sparse_identity_do_not_clear_existing_names(store):
    fake,j=store
    from dataclasses import replace
    mapping={'source_property_id':'ID','real_account_number':'Account','situs_address':'Address','owner_name':'Owner','dba_name':'DBA','tax_year':'Year'}
    j=replace(j,property_field_mapping=mapping)
    first='ID,Account,Address,Owner,DBA,Year\n1,P1,100 MAIN ST,ACME,ACME SHOP,2026\n2,P2,200 MAIN ST,,,2026\n'
    r=property_import.import_property_csv(j,first,source_as_of_date='2026-10-01')
    assert r.quality_summary['named_bpp_record_count']==1
    assert r.warnings
    assert fake.property_source_imports[r.import_id]['import_status']=='COMPLETED'
    property_import.import_property_csv(j,first.replace('ACME,ACME SHOP',','))
    assert fake.real_property_records['jur-1::1::2026']['owner_name']=='ACME'
    assert fake.real_property_records['jur-1::1::2026']['dba_name']=='ACME SHOP'


def test_preview_allows_unknown_column_mapping_and_validates_missing_headers(store):
    preview=property_import.preview_property_file(store[1],b'customid,customaccount,customaddress\n1,P1,100 MAIN ST\n','test.csv')
    assert preview['result'] is None
    assert preview['validation_error']
    assert len(store[0].property_source_imports)==0
    with pytest.raises(property_import.PropertyImportError):
        property_import.import_property_csv(store[1],'PropertyID,QuickRefID\n1,P1\n')
    assert len(store[0].property_source_imports)==0


def test_failed_import_is_not_marked_complete(store,monkeypatch):
    def fail(_):raise service.ComptrollerServiceError('write failed')
    monkeypatch.setattr(property_import,'_upsert_property_rows',fail)
    with pytest.raises(service.ComptrollerServiceError):
        property_import.import_property_csv(store[1],'PropertyID,QuickRefID,SitusAddress,SitusZip,TUG,NBHD,MapID\n1,P1,100 MAIN ST,79401,1,2,3\n')
    assert list(store[0].property_source_imports.values())[0]['import_status']=='FAILED'


def test_health_reports_stale_source_missing_names_and_disjoint_match_rates(store,monkeypatch):
    fake,j=store
    fake.property_source_imports['old']={'id':'old','jurisdiction_id':j.id,'import_status':'COMPLETED','imported_at':'2026-06-01T00:00:00Z','source_as_of_date':'2026-06-01','row_count':2}
    fake.property_source_imports['failed']={'id':'failed','jurisdiction_id':j.id,'import_status':'FAILED','imported_at':'2026-10-23T00:00:00Z','row_count':1}
    monkeypatch.setattr(cad_review,'_count_properties',lambda _,**filters:0 if 'or' in filters else 2)
    data=cad_review.match_health(j,[make_item(confidence='LOW',is_ambiguous=True),make_item(confidence='HIGH'),make_item(confidence='UNMATCHED')],today=date(2026,10,23))
    assert data['freshness']=='STALE'
    assert data['latest_import']['id']=='old'
    assert data['name_coverage_percent']==0
    assert data['queue']=={'total':3,'high_confidence':1,'possible':0,'ambiguous':1,'unmatched':1}


def test_rejecting_confirmation_replaces_stale_human_confirmed_signals(store,monkeypatch):
    from app.comptroller import intelligence
    fake,j=store;prop=seed(store)
    row=asdict(item());row['source_permit_location_id']='permit-1'
    fake.intelligence_items[row['id']]=row
    monkeypatch.setattr(matching,'fetch_candidate_records',lambda _:[])
    decide(store,prop,'CONFIRMED')
    cad_review.refresh_item_match(item())
    assert fake.intelligence_items['intel-1']['match_signals']['human_decision']=='CONFIRMED'
    decide(store,prop,'REJECTED')
    cad_review.refresh_item_match(item())
    assert 'human_decision' not in fake.intelligence_items['intel-1']['match_signals']
    assert fake.intelligence_items['intel-1']['status']=='NEW'


def test_background_refresh_keeps_decisions_and_saves_import_match_snapshot(store,monkeypatch):
    from app.comptroller import intelligence
    fake,j=store;prop=seed(store)
    row=asdict(item());row['source_permit_location_id']='permit-1'
    fake.intelligence_items[row['id']]=row
    fake.property_source_imports['import-1']={'id':'import-1','jurisdiction_id':j.id,'import_status':'COMPLETED'}
    monkeypatch.setattr(intelligence,'_request_json',fake.request_json)
    monkeypatch.setattr(matching,'fetch_candidate_records',lambda _:[])
    decide(store,prop,'CONFIRMED')
    cad_review.refresh_after_import(j,'import-1',{'bpp_record_count':1})
    saved=fake.property_source_imports['import-1']['quality_summary']
    assert saved['matching_refresh_status']=='COMPLETED'
    assert saved['queue_match_snapshot']['high_confidence']==1
    assert fake.intelligence_items['intel-1']['status']=='NEW'
    assert len(fake.cad_match_decisions)==1


def test_queue_metrics_do_not_stop_at_500_rows(store,monkeypatch):
    from app.comptroller import intelligence
    fake,j=store
    for index in range(510):
        row=asdict(make_item(item_id=f'item-{index}'))
        fake.intelligence_items[row['id']]=row
    monkeypatch.setattr(intelligence,'_request_json',fake.request_json)
    assert intelligence.get_queue_summary(j.district_id)['total']==510


def test_rejection_comparison_normalizes_account_punctuation(store):
    prop=seed(store,owner='ACME')
    decide(store,prop,'REJECTED')
    result=match(store,candidates=[matching.MatchCandidate('rendition-1','p-1','ACME',2026)])
    assert result.candidate is None


def test_admin_import_saves_mapping_and_schedules_matching_refresh(store,monkeypatch):
    import base64
    fake,j=store
    monkeypatch.setattr(backend,'require_bpp_access',lambda _:make_district())
    payload=backend.CadImportRequest(access_token='x',filename='test.csv',source_as_of_date='2026-10-01',
        file_base64=base64.b64encode(b'ID,Account,Address,Owner\n1,P1,100 MAIN ST,ACME\n').decode(),
        mapping={'source_property_id':'ID','real_account_number':'Account','situs_address':'Address','owner_name':'Owner'})
    tasks=backend.BackgroundTasks()
    result=backend.intelligence_cad_import(payload,tasks)
    assert result['matching_refresh_status']=='PENDING'
    assert len(tasks.tasks)==1
    assert fake.jurisdictions[j.id]['property_field_mapping']==payload.mapping
    assert list(fake.property_source_imports.values())[0]['quality_summary']['matching_refresh_status']=='PENDING'


def test_omitted_active_export_account_is_excluded_without_erasing_history(store):
    fake,j=store
    prop=seed(store,owner='ACME')
    decide(store,prop,'CONFIRMED')
    fake.real_property_records['P1']['account_status']='NOT_IN_ACTIVE_DBA_EXPORT'
    assert match(store).candidate is None
    assert cad_review.candidate_review(j,item())['candidates']==[]
    with pytest.raises(cad_review.CadReviewError,match='current active DBA'):
        decide(store,prop,'CONFIRMED')
    assert len(fake.cad_match_decisions)==1
    assert property_adapter.get_property_adapter(j).get_property_by_id(j,prop.property_id) is not None


def test_active_export_account_still_matches(store):
    seed(store,owner='ACME',account_status='A')
    assert match(store).candidate.account_number=='P1'
