from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from app.comptroller import mailing_address_intelligence as legacy, mailing_comparisons as comparisons
from app.comptroller.property_adapter import _row_to_property
from tests.comptroller_fakes import FakeSupabase
from tests.test_mailing_address_intelligence import seed_jurisdiction, add_permit


@pytest.fixture
def setup(monkeypatch):
    fake=FakeSupabase()
    monkeypatch.setattr(legacy,'get_supabase_config',lambda:('https://fake.supabase.co','fake'))
    monkeypatch.setattr(legacy,'_request_json',fake.request_json)
    j=seed_jurisdiction(fake)
    monkeypatch.setattr(comparisons,'get_jurisdiction',lambda _:j)
    prop=_row_to_property(dict(id='cad1',jurisdiction_id=j.id,source_property_id='1',real_account_number='P1',
        mailing_address_line1='100 Main St',mailing_city='Lubbock',mailing_state='TX',mailing_zip='79401',
        mailing_recipient='Acme Mail Department',mailing_imported_at='2026-10-01T00:00:00Z',mailing_source_as_of_date='2026-09-30'))
    adapter=SimpleNamespace(get_property_by_id=Mock(return_value=prop))
    monkeypatch.setattr(comparisons,'get_property_adapter',lambda _:adapter)
    diagnostic=Mock(return_value={'link':{'matched_record_id':'cad1','matched_account_number':'P1','matched_owner_name':'Acme'},
        'identity_confidence':'HIGH','reason':'RELIABLE_ACCOUNT_MATCH','locations':[{'confidence':'HIGH','reason':'situs and owner match'}]})
    monkeypatch.setattr(comparisons,'diagnose_account_link',diagnostic)
    return fake,j,adapter,diagnostic


def scan(setup, address='200 Main St', **fields):
    fake,j,_,_=setup
    add_permit(fake,'T1','1',mailing_address=address,**fields)
    return comparisons.run_scan(j.id)


def item(setup):
    return next(iter(setup[0].intelligence_items.values()))


def test_first_scan_compares_crs_even_without_prior_observations(setup):
    result=scan(setup)
    assert result.items_created==1
    row=item(setup)
    assert row['mailing_address_current']=='100 MAIN STREET LUBBOCK TX 79401'
    assert row['evidence']['crs']['recipient']=='Acme Mail Department'
    assert row['evidence']['account_match']['locations'][0]['reason']=='situs and owner match'
    assert row['evidence']['postal_validation']=='NOT_PERFORMED'
    assert row['evidence']['comptroller']['source_effective_date'] is None
    assert row['evidence']['correction_supported'] is False


def test_unchanged_source_still_detects_newly_imported_crs_discrepancy(setup):
    scan(setup,'100 Main Street')
    assert not setup[0].intelligence_items
    setup[2].get_property_by_id.return_value=replace(setup[2].get_property_by_id.return_value,mailing_address_line1='500 Oak Ave')
    result=scan(setup,'100 Main Street')
    assert result.items_created==1 and result.observations_added==0


def test_formatting_and_zip4_addition_do_not_create_flags(setup):
    result=scan(setup,'100 Main Street',mailing_zip='79401-1234')
    assert result.same_address==1 and not setup[0].intelligence_items


def test_repeated_scans_refresh_one_open_flag_and_preserve_review(setup):
    scan(setup)
    row=item(setup);row.update(status='IN_REVIEW',resolution_notes='Staff research',assigned_to='appraiser')
    result=scan(setup)
    assert result.items_created==0 and result.items_updated==1
    assert len(setup[0].intelligence_items)==1
    assert row['status']=='IN_REVIEW' and row['resolution_notes']=='Staff research' and row['assigned_to']=='appraiser'


def test_resolved_same_facts_not_reset_by_fresh_import_timestamps(setup):
    scan(setup)
    row=item(setup);row.update(status='RESOLVED',resolution='CURRENT_ADDRESS_CORRECT')
    setup[2].get_property_by_id.return_value=replace(setup[2].get_property_by_id.return_value,mailing_imported_at='2026-11-02T00:00:00Z')
    result=scan(setup)
    assert result.duplicates_suppressed==1 and row['status']=='RESOLVED'
    assert len(setup[0].intelligence_items)==1


def test_a_b_a_chronology_and_new_event_after_resolution(setup):
    scan(setup,'200 Main St');first=item(setup);first['status']='RESOLVED'
    scan(setup,'300 Main St');second=list(setup[0].intelligence_items.values())[-1];second['status']='RESOLVED'
    scan(setup,'200 Main St')
    assert len(setup[0].mailing_address_observations)==3
    assert list(setup[0].mailing_address_observations.values())[-1]['raw_address_line']=='200 Main St'
    assert len(setup[0].intelligence_items)==3
    result=scan(setup,'200 Main St')
    assert result.items_created==0 and result.observations_added==0
    assert first['status']==second['status']=='RESOLVED'


@pytest.mark.parametrize('fields',[{'mailing_city':None},{'mailing_state':None},{'mailing_zip':'123'},{'mailing_address':None}])
def test_incomplete_source_is_review_gap_never_correction(setup,fields):
    address=fields.pop('mailing_address','200 Main St')
    result=scan(setup,address,**fields)
    row=item(setup)
    assert row['evidence']['comparison_status']=='INCOMPLETE_DATA'
    assert row['priority']=='MEDIUM' and row['evidence']['correction_supported'] is False
    assert result.observations_added==0


def test_missing_crs_shows_explicit_review_gap_without_invented_current_address(setup):
    setup[2].get_property_by_id.return_value=replace(setup[2].get_property_by_id.return_value,mailing_address_line1=None)
    result=scan(setup)
    assert result.missing_crs==1
    assert item(setup)['evidence']['comparison_status']=='MISSING_CRS_MAILING'
    assert item(setup)['evidence']['crs']['line1'] is None


def test_source_now_agrees_does_not_automatically_complete_open_review(setup):
    scan(setup)
    scan(setup,'100 Main St')
    row=item(setup)
    assert row['status']=='NEW' and row['evidence']['comparison_status']=='NO_CURRENT_DISCREPANCY'


def test_unlinked_taxpayer_not_forced_into_queue_and_counted(setup):
    setup[3].return_value={'link':{'matched_record_id':None},'reason':'AMBIGUOUS_LOCATION'}
    result=scan(setup)
    assert result.unlinked==1 and not setup[0].intelligence_items


def test_zero_change_scan_has_durable_success_and_counts(setup):
    result=scan(setup,'100 Main St')
    run=setup[0].mailing_scan_runs[result.run_id]
    assert run['status']=='SUCCESS' and run['records_examined']==1
    assert run['flags_created']==0 and run['observations_added']==1
    assert run['finished_at'] and run['county']=='Test'


def test_record_failure_is_durable_without_pii_and_other_records_continue(setup):
    setup[3].side_effect=RuntimeError('Private address must not leak')
    with pytest.raises(legacy.MailingAddressIntelligenceError):scan(setup)
    run=next(iter(setup[0].mailing_scan_runs.values()))
    assert run['status']=='FAILED' and run['records_examined']==1
    assert run['errors'][0]['error_type']=='RuntimeError'
    assert 'Private' not in str(run['errors'])


def test_fetch_failure_finishes_started_run(setup,monkeypatch):
    monkeypatch.setattr(legacy,'get_mailing_address_candidates',Mock(side_effect=RuntimeError('source unavailable')))
    with pytest.raises(RuntimeError):comparisons.run_scan(setup[1].id)
    run=next(iter(setup[0].mailing_scan_runs.values()))
    assert run['status']=='FAILED' and run['records_examined']==0


def test_dry_run_does_not_write_ledger_observations_state_or_flags(setup):
    add_permit(setup[0],'T1','1',mailing_address='200 Main St')
    comparisons.run_scan(setup[1].id,dry_run=True)
    assert not any(c['method']!='GET' for c in setup[0].calls)


def test_multiple_active_locations_with_conflicting_addresses_are_not_arbitrarily_selected(setup):
    add_permit(setup[0],'T1','2',mailing_address='500 Oak Ave')
    scan(setup)
    assert item(setup)['evidence']['comparison_status']=='INCOMPLETE_DATA'
    assert not setup[0].mailing_address_observations


def test_inactive_permits_not_compared(setup):
    add_permit(setup[0],'T1','1',current_status='INACTIVE')
    result=comparisons.run_scan(setup[1].id)
    assert result.evaluated==0
    assert setup[0].mailing_scan_runs[result.run_id]['status']=='SUCCESS'


@pytest.mark.parametrize('crs,observed,expected',[
    ({'line1':'PO Box 100'},{'line1':'P.O. Box 100'},'NO_CURRENT_DISCREPANCY'),
    ({'line1':'PO Box 100'},{'line1':'PO Box 200'},'DISCREPANCY'),
    ({'line1':'100 Main St','line2':'Suite 200'},{'line1':'100 Main Street STE 200'},'NO_CURRENT_DISCREPANCY'),
    ({'line1':'100 Main St','unit':'200'},{'line1':'100 Main St STE 500'},'DISCREPANCY'),
    ({'line1':'100 Main St, STE 200'},{'line1':'100 Main St STE 200'},'NO_CURRENT_DISCREPANCY'),
    ({'line1':'100 Main St','line2':'C/O JANE'},{'line1':'100 Main St','line2':'C/O JOHN'},'DISCREPANCY'),
    ({'line1':'100 Main St','zip_code':'79401-1234'},{'line1':'100 Main St','zip_code':'79401-5678'},'DISCREPANCY'),
    ({'line1':'100 Main St','unit':'500'},{'line1':'100 Main St'},'DISCREPANCY'),
    ({'line1':'100 Main St','city':None},{'line1':'200 Main St'},'INCOMPLETE_DATA'),
])
def test_component_comparisons_preserve_meaning_without_format_alerts(crs,observed,expected):
    def snap(values):return comparisons.snapshot(**{'city':'Lubbock','state':'TX','zip_code':'79401',**values})
    assert comparisons.compare_snapshots(snap(crs),snap(observed))[0]==expected


def test_later_unreliable_match_marks_open_flag_without_forcing_link_or_losing_notes(setup):
    scan(setup);row=item(setup);row['status']='IN_REVIEW';row['resolution_notes']='Keep research'
    setup[3].return_value={'link':{'matched_record_id':None},'reason':'AMBIGUOUS_LOCATION'}
    result=scan(setup)
    assert result.unlinked==1 and result.items_updated==1
    assert row['evidence']['comparison_status']=='ACCOUNT_MATCH_NEEDS_REVIEW'
    assert row['confidence']=='LOW' and row['status']=='IN_REVIEW' and row['resolution_notes']=='Keep research'


@pytest.mark.parametrize('zip_code',['1234','123456','AB12345','79401-XX'])
def test_malformed_zip_is_not_a_supported_correction(zip_code):
    result=comparisons.snapshot('100 Main St',city='Lubbock',state='TX',zip_code=zip_code)
    assert result['complete'] is False and result['postal_validation']=='NOT_PERFORMED'


def test_conflicting_explicit_and_inline_units_are_incomplete():
    result=comparisons.snapshot('100 Main St STE 100',unit='500',city='Lubbock',state='TX',zip_code='79401')
    assert not result['complete'] and 'Conflicting unit fields' in result['incomplete_reasons']


def test_unit_number_sign_after_suite_marker_is_formatting_only():
    def snap(line,**kw):return comparisons.snapshot(line,city='Lubbock',state='TX',zip_code='79401',**kw)
    assert comparisons.compare_snapshots(snap('100 Main St, Suite #200'),snap('100 MAIN STREET',unit='200'))[0]=='NO_CURRENT_DISCREPANCY'
