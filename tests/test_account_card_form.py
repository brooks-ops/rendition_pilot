import pytest
from app.comptroller.account_card_form import form,validate,total,NewAccountEnrichmentError
from tests.test_new_account_enrichment import make_item,make_jurisdiction

SUMMARY={'appraiser':{'appraiser':None},'exceptions':[],'personal_property_accounts':[]}

def test_reference_example_is_never_a_new_account_default():
    card=form(make_item(),make_jurisdiction(slug='lubbock'),SUMMARY)
    d=card['data'];assert card['profile']=='lcad'
    assert d['business_name']=="JOE'S SPORTS BAR"
    assert d['appraiser']==d['mailing_address']==d['map_id']==d['sic']==d['property_id']==''
    assert all(not any(row.values()) for row in d['segments']) and card['total_value'] is None
    assert d['situs_address']=='1234 MAIN ST' and d['permit_issued']=='2026-08-01'


def test_dba_uses_actual_mailing_and_never_calls_recording_date_a_permit_date():
    item=make_item(signal_type='county_clerk_dba',permit_start_date=None,raw={'filing_type':'ASSUMED NAME','source_taxpayer_id':'00123456789','evidence':{'filing':{'recording_date':'2026-01-02','mailing_address':{'line1':'PO BOX 1','city':'OTHER CITY','zip':'00123'}}}})
    d=form(item,make_jurisdiction(),SUMMARY)['data']
    assert d['mailing_address']=='PO BOX 1' and d['mailing_zip']=='00123'
    assert d['permit_issued']=='' and d['sales_tax_id']=='00123456789'


def test_saved_blanks_codes_and_zero_values_survive_reopening():
    saved=validate({'business_name':'','neighborhood':'0722','sic':'0012','segments':[{'flat_value':'0.00'},{'flat_value':'0.10'},{'flat_value':'0.20'}]})
    item=make_item(raw={'account_card_draft':saved,'account_card_version':3,'account_card_saved_at':'saved'})
    card=form(item,make_jurisdiction(),SUMMARY)
    assert card['data']['business_name']=='' and card['data']['neighborhood']=='0722'
    assert card['total_value']=='0.30' and card['version']==3
    assert total(validate({'segments':[{'flat_value':'0'}]}))=='0.00'
    assert total(validate({'segments':[{'flat_value':''}]})) is None

@pytest.mark.parametrize('data',[{'segments':[{'flat_value':'nan'}]},{'segments':[{'flat_value':'1.001'}]},{'segments':[{}]*21},{'unknown':'bad'},{'last_inspected':'tomorrow'},{'tax_year':'26'}])
def test_invalid_drafts_are_rejected(data):
    with pytest.raises(NewAccountEnrichmentError):validate(data)

def test_save_endpoint_uses_authenticated_district_and_blocks_stale_edits(monkeypatch):
    from backend import main
    from tests.test_backend_intelligence_queue import make_district
    from fastapi import HTTPException
    from app.comptroller.service import ComptrollerServiceError
    monkeypatch.setattr(main,'require_bpp_access',lambda token:make_district())
    monkeypatch.setattr(main,'_require_own_district_item',lambda *a:make_item())
    monkeypatch.setattr(main,'_claim_bpp_work',lambda *a:None)
    monkeypatch.setattr(main.activity_analytics,'record_activity',lambda *a,**kw:None)
    def save(method,path,**kw):
        p=kw['payload'];assert p['p_district_id']=='district-1' and p['p_actor_id']=='user-1'
        raise ComptrollerServiceError('CARD_VERSION_CONFLICT')
    monkeypatch.setattr(main.comptroller_cad_review,'_request',save)
    with pytest.raises(HTTPException) as exc:main.intelligence_account_card_save(main.AccountCardSaveRequest(access_token='test',source_table='bpp_intelligence_items',item_id='intel-1',version=1,data={}))
    assert exc.value.status_code==409


def test_single_permit_date_preserves_legacy_drafts():
    item=make_item(raw={'account_card_draft':{'permit_issued':'2026-08-01','additional_permit_date':'2026-09-01'}})
    card=form(item,make_jurisdiction(),SUMMARY)
    assert card['data']['permit_issued']=='2026-08-01'
    assert 'additional_permit_date' not in card['data']
    permit_fields=[f['name'] for section in card['sections'] for f in section['fields'] if 'permit' in f['name'] and f['type']=='date']
    assert permit_fields==['permit_issued']
    with pytest.raises(NewAccountEnrichmentError):
        validate({'additional_permit_date':'2026-09-01'})


def test_completed_cards_are_scoped_to_authenticated_appraiser_and_district(monkeypatch):
    from backend import main
    from tests.test_backend_intelligence_queue import make_district
    monkeypatch.setattr(main,'require_bpp_access',lambda token:make_district())
    def fetch(method,table,params):
        assert table=='bpp_intelligence_items'
        assert params['district_id']=='eq.district-1' and params['reviewed_by']=='eq.user-1'
        assert params['status']=='eq.RESOLVED' and params['account_card_draft']=='not.is.null'
        assert params['offset']=='100'
        return [{'id':str(i),'account_card_draft':{'business_name':'Saved business','segments':[]},'account_card_version':2} for i in range(101)]
    monkeypatch.setattr(main.comptroller_cad_review,'_request',fetch)
    result=main.completed_account_cards(main.CompletedCardsRequest(access_token='test',offset=100))
    assert len(result['cards'])==100 and result['next_offset']==200
    assert result['cards'][0]['data']['business_name']=='Saved business'
