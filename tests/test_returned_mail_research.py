from types import SimpleNamespace
from unittest.mock import Mock
from dataclasses import replace
import pytest
from app import returned_mail_research as research
from app.comptroller.property_adapter import _row_to_property

@pytest.fixture
def prop():
    return _row_to_property({'id':'record','jurisdiction_id':'j','source_property_id':'1','real_account_number':'P123',
        'business_name':'Sample Business LLC','owner_name':'Sample Owner','mailing_recipient':'Sample Business LLC',
        'mailing_address_line1':'10 Main St','mailing_city':'Lubbock','mailing_state':'TX','mailing_zip':'79401'})

@pytest.fixture
def setup(monkeypatch,prop):
    j=SimpleNamespace(id='j',district_id='d',county_name='Lubbock',current_tax_year=None)
    adapter=SimpleNamespace(find_properties_by_real_account=Mock(return_value=[prop]))
    monkeypatch.setattr(research,'get_property_adapter',lambda _:adapter)
    monkeypatch.setattr(research,'related_accounts',lambda *_:([],[]))
    monkeypatch.setattr(research,'comptroller_leads',lambda *_:([],[]))
    web=Mock(return_value={'status':'NO_RELIABLE_SOURCE','sources':[],'summary':'No reliable source.'})
    monkeypatch.setattr(research,'web_research',web)
    return j,adapter,web


def doc(**kw):return {'account_number':'P123','uploaded_at':'2026-11-09',**kw}


def test_explicit_forwarding_retained_without_fabricating_missing_parts(setup):
    j,_,web=setup
    result,status=research.build_research(doc(suggested_forwarding='100 New Street'),j)
    assert status=='COMPLETE'
    assert result['leads'][0]['raw_address']=='100 New Street'
    assert 'address' not in result['leads'][0]
    assert result['postal_validation']=='NOT_PERFORMED' and not result['correction_supported']
    web.assert_called_once()


def test_wrong_account_blocks_external_and_internal_research(setup,monkeypatch):
    j,adapter,web=setup
    lookup=Mock();monkeypatch.setattr(research,'related_accounts',lookup)
    result,status=research.build_research(doc(account_mismatch=True),j)
    assert status=='SKIPPED'
    adapter.find_properties_by_real_account.assert_not_called();web.assert_not_called();lookup.assert_not_called()


def test_ambiguous_import_not_researched(setup):
    j,adapter,web=setup
    adapter.find_properties_by_real_account.return_value*=2
    assert research.build_research(doc(),j)[1]=='SKIPPED'
    web.assert_not_called()


def test_internal_failure_does_not_hide_forwarding_or_web_leads(setup,monkeypatch):
    j,_,web=setup
    monkeypatch.setattr(research,'related_accounts',Mock(side_effect=RuntimeError('secret')))
    result,status=research.build_research(doc(suggested_forwarding='PO BOX 123'),j)
    assert status=='PARTIAL' and result['leads']
    assert 'secret' not in str(result)
    web.assert_called_once()


def test_complete_linked_discrepancy_avoids_unnecessary_web_work(setup,monkeypatch):
    j,_,web=setup
    lead={'kind':'COMPTROLLER','address':{'complete':True},'comparison_status':'DISCREPANCY'}
    monkeypatch.setattr(research,'comptroller_leads',lambda *_:([lead],[]))
    result,status=research.build_research(doc(),j)
    assert result['web']['status']=='NOT_NEEDED'
    web.assert_not_called()


def test_current_comptroller_match_must_be_reconfirmed_before_mailing_access(prop,monkeypatch):
    j=SimpleNamespace(id='j',district_id='d',county_name='Lubbock')
    query=Mock(return_value=[{'source_taxpayer_id':'taxpayer'}]);monkeypatch.setattr(research.mail,'request',query)
    monkeypatch.setattr(research,'diagnose_account_link',lambda *_:{'link':{'matched_account_number':'P999'}})
    leads,warnings=research.comptroller_leads(j,prop)
    assert not leads and warnings and query.call_count==1
    assert query.call_args.kwargs['params']['district_id']=='eq.d'


def test_incomplete_comptroller_address_stays_incomplete(prop,monkeypatch):
    j=SimpleNamespace(id='j',district_id='d',county_name='Lubbock')
    query=Mock(side_effect=[[{'source_taxpayer_id':'t'}],[{'mailing_address':'100 Main St','legal_name':'Sample','last_checked_at':'2026-11-01'}]])
    monkeypatch.setattr(research.mail,'request',query)
    monkeypatch.setattr(research,'diagnose_account_link',lambda *_:{'link':{'matched_account_number':'P123'},'identity_confidence':'HIGH'})
    leads,_=research.comptroller_leads(j,prop)
    assert leads[0]['address']['complete'] is False
    assert leads[0]['comparison_status']=='INCOMPLETE_DATA'
    assert query.call_args.kwargs['params']['county']=='eq.Lubbock'


def response(sources=True,searched=True):
    raw={'status':'completed','output':([{'type':'web_search_call','status':'completed'}] if searched else [])+[
        {'type':'message','content':[{'annotations':[{'type':'url_citation','url':'https://example.com/contact','title':'Contact'}] if sources else []}]}]}
    return SimpleNamespace(model_dump=lambda:raw,output_text='A business LOCATION, mailing purpose unconfirmed.')


def test_web_requires_real_search_and_citations_and_sends_only_business_context(prop):
    client=Mock();client.responses.create.return_value=response()
    result=research.web_research(prop,client)
    assert result['status']=='LEADS_FOUND' and result['sources'][0]['url']=='https://example.com/contact'
    args=client.responses.create.call_args.kwargs
    assert args['tool_choice']=='required' and not args['store']
    assert '10 Main St' not in args['input'] and 'Sample Owner' not in args['input']
    client.responses.create.return_value=response(sources=False)
    assert research.web_research(prop,client)['status']=='NO_RELIABLE_SOURCE'
    client.responses.create.return_value=response(searched=False)
    with pytest.raises(ValueError):research.web_research(prop,client)


def test_missing_key_and_business_name_are_explicit(prop,monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY',raising=False);monkeypatch.delenv('MAILING_RESEARCH_API_KEY',raising=False)
    assert research.web_research(prop)['status']=='NOT_CONFIGURED'
    client=Mock()
    assert research.web_research(replace(prop,business_name=None,dba_name=None),client)['status']=='INSUFFICIENT_IDENTITY'
    client.responses.create.assert_not_called()


@pytest.mark.parametrize('url',['javascript:alert(1)','https://user:secret@example.com','http://127.0.0.1','http://169.254.169.254','http://localhost','http://x.internal','https://example.com:9000','https://[invalid'])
def test_source_urls_reject_unsafe_targets(url):assert research.safe_url(url) is None


def test_worker_scoped_claim_and_token_finish_preserve_review(setup,monkeypatch):
    j,_,_=setup
    monkeypatch.setattr(research,'get_jurisdiction_by_district_id',lambda _:j)
    calls=[]
    def request(method,table,**kw):
        calls.append((method,table,kw))
        if table.endswith('claim_research'):return {'document_id':'doc','lease_token':'token'}
        if table=='returned_mail_documents':return [doc()]
        return True
    monkeypatch.setattr(research.mail,'request',request)
    assert research.process_pending('d','doc',limit=1)=={'processed':1,'failed':0}
    assert calls[0][2]['payload']=={'p_district_id':'d','p_document_id':'doc'}
    assert calls[1][2]['params']['district_id']=='eq.d'
    assert calls[-1][2]['payload']['p_token']=='token'
    assert all(table in ('rpc/returned_mail_claim_research','returned_mail_documents','rpc/returned_mail_finish_research') for _,table,_ in calls)


def test_related_account_name_only_is_low_confidence_and_scoped(prop,monkeypatch):
    j=SimpleNamespace(id='j',current_tax_year=None)
    row={'id':'r2','jurisdiction_id':'j','source_property_id':'2','real_account_number':'P999',
         'owner_name':'Sample Owner','mailing_address_line1':'200 Other St','mailing_city':'Lubbock','mailing_state':'TX','mailing_zip':'79401'}
    query=Mock(return_value=[row]);monkeypatch.setattr(research.mail,'request',query)
    monkeypatch.setattr(research,'get_property_adapter',lambda _:SimpleNamespace(find_properties_by_real_account=lambda *_:[_row_to_property(row)]))
    leads,_=research.related_accounts(j,prop)
    assert len(leads)==1 and leads[0]['identity_confidence']=='LOW'
    assert all(c.kwargs['params']['jurisdiction_id']=='eq.j' for c in query.call_args_list)
    assert research.exact_name_pattern('A*%_B')=='A\\*\\%\\_B'


def test_daily_retry_uses_configured_app_not_ai_provider_and_never_follows_redirects(monkeypatch):
    import requests
    monkeypatch.setattr(research.mail,'get_supabase_config',lambda:('https://db.example','service-secret'))
    monkeypatch.delenv('MAILING_RESEARCH_APP_URL',raising=False)
    post=Mock(return_value=SimpleNamespace(status_code=202));monkeypatch.setattr(requests,'post',post)
    research.dispatch_retry('district')
    assert post.call_args.args[0]=='https://www.renditionpilot.com/api/internal/returned-mail/research'
    assert post.call_args.kwargs['json']=={'district_id':'district'}
    assert post.call_args.kwargs['allow_redirects'] is False
    monkeypatch.setenv('MAILING_RESEARCH_APP_URL','http://insecure.example')
    with pytest.raises(ValueError):research.dispatch_retry('district')


def test_historical_recipient_does_not_label_current_account_as_related(prop,monkeypatch):
    j=SimpleNamespace(id='j',current_tax_year=2027)
    historic={'id':'r2','jurisdiction_id':'j','source_property_id':'2','real_account_number':'P999','tax_year':2025,
              'owner_name':prop.owner_name,'mailing_address_line1':'200 Other St'}
    monkeypatch.setattr(research.mail,'request',Mock(return_value=[historic]))
    current=_row_to_property({**historic,'id':'new','tax_year':2027,'owner_name':'Different recipient'})
    monkeypatch.setattr(research,'get_property_adapter',lambda _:SimpleNamespace(find_properties_by_real_account=lambda *_:[current]))
    assert research.related_accounts(j,prop)[0]==[]
