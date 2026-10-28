from pathlib import Path
from types import SimpleNamespace
import pytest,requests
from app.county_clerk.public_client import PublicClient,SourceUnavailable,results,BASE

def html(*,first=1,last=1,total=1,next_url=None):
    count=f'{total} items found, displaying {first} to {last}.'
    rows=''.join(f'<tr><td>ASSUMED NAME<br>{2026700000+i}</td><td><a href="viewDoc.jsp?node={i}">Source</a></td><td></td><td></td></tr>' for i in range(first,last+1))
    return count+f'<table id="searchResultsTable"><tbody>{rows}</tbody></table>'+ (f'<a href="{next_url}">Next</a>' if next_url else '')

def test_count_and_pagination_must_reconcile():
    rows,total,first,last,next_url=results(html(first=1,last=100,total=1160,next_url='docSearchResults.jsp?page=2'),BASE+'/recorder/eagleweb/docSearchResults.jsp')
    assert len(rows)==100 and (total,first,last)==(1160,1,100)
    assert next_url.endswith('page=2') and rows[0]['description']=='ASSUMED NAME\n2026700001'
    with pytest.raises(SourceUnavailable,match='pagination'):results(html(total=2),BASE+'/recorder/eagleweb/docSearchResults.jsp')
    with pytest.raises(SourceUnavailable,match='3,000'):results(html(total=3001),BASE+'/recorder/eagleweb/docSearchResults.jsp')

def test_does_not_follow_foreign_source_links():
    with pytest.raises(SourceUnavailable,match='host'):results(html().replace('viewDoc.jsp?node=1','https://foreign.example/doc'),BASE+'/recorder/eagleweb/docSearchResults.jsp')

@pytest.mark.parametrize('status,body',[(403,''),(429,''),(200,'<div class="g-recaptcha">'),(200,'Verify you are human')])
def test_stops_on_restrictions_without_retry(status,body):
    calls=[]
    def send(*a,**kw):
        calls.append(kw)
        return SimpleNamespace(status_code=status,text=body,content=body.encode(),raise_for_status=lambda:None)
    session=SimpleNamespace(headers={},request=send)
    c=PublicClient(session=session)
    with pytest.raises(SourceUnavailable):c.fetch('GET',BASE+'/recorder/web/splash.jsp')
    assert len(calls)==1 and calls[0]['allow_redirects'] is False
    assert 'verify' not in calls[0]

def test_cannot_remove_minimum_delay():
    assert PublicClient(interval=0).interval==2.0

def test_notice_change_stops_before_guest_acknowledgement(monkeypatch):
    c=PublicClient();calls=[]
    def fetch(method,url,**kw):
        calls.append((method,url))
        if url.endswith('robots.txt'):return SimpleNamespace(text='User-agent: *\nAllow: /')
        return SimpleNamespace(text='<div id="middle"><p>The County Clerk\'s Office, Lubbock County, Texas provides NEW TERMS</p></div>')
    monkeypatch.setattr(c,'fetch',fetch)
    with pytest.raises(SourceUnavailable,match='notice changed'):c.connect()
    assert all(method=='GET' for method,url in calls)

def test_detail_cache_requires_same_identity_url_and_complete_summary():
    from app.county_clerk.detail_cache import same_summary
    entry={'description':'ASSUMED NAME\n2026700001','url':BASE+'/recorder/eagleweb/viewDoc.jsp?node=1','summary':'Owner(s): SOMEONE\nExpires: 01/01/2036'}
    assert same_summary(entry,{**entry,'summary':'Owner(s): SOMEONE Expires: 01/01/2036'})
    assert not same_summary(entry,{**entry,'summary':'Owner(s): DIFFERENT Expires: 01/01/2036'})
    assert not same_summary(entry,{**entry,'url':BASE+'/recorder/eagleweb/viewDoc.jsp?node=2'})
