"""Low-rate public guest HTML collection. No private API or control bypass."""
import json,re,time
from datetime import date,datetime,timezone
from pathlib import Path
from urllib.parse import urljoin,urlparse
from urllib.robotparser import RobotFileParser
import requests
from bs4 import BeautifulSoup

BASE='https://erecord.lubbockcounty.gov'
SPLASH=BASE+'/recorder/web/splash.jsp'
AGENT='RenditionPilot/1.0 (+https://www.renditionpilot.com)'
MAX_RECORDS=3000
class SourceUnavailable(RuntimeError):pass

def soup_text(soup):return re.sub(r'\s+',' ',soup.get_text(' ',strip=True)).strip()
def trusted(url):
    u=urlparse(url)
    if u.scheme!='https' or u.netloc!='erecord.lubbockcounty.gov' or not (u.path.startswith('/recorder/') or u.path=='/robots.txt'):
        raise SourceUnavailable('Source returned an unexpected host or path')
    return url

def results(html,url):
    soup=BeautifulSoup(html,'html.parser');text=soup.get_text(' ',strip=True)
    match=re.search(r'([\d,]+) items found, displaying (?:([\d,]+) to ([\d,]+)|(all items))',text)
    if not match:
        if re.search(r'\b(?:0 items found|No (?:items|records|documents|results) found)\b',text,re.I):return [],0,1,0,None
        raise SourceUnavailable('Search result count is missing or changed')
    total=int(match[1].replace(',',''));first=int((match[2] or '1').replace(',',''));last=int((match[3] or str(total)).replace(',',''))
    if total>MAX_RECORDS:raise SourceUnavailable('Source exceeds 3,000 filings; split the collection window')
    table=soup.find(id='searchResultsTable')
    if table is None:raise SourceUnavailable('Search results table is missing')
    rows=[]
    for tr in table.select(':scope > tbody > tr'):
        cells=tr.find_all('td',recursive=False)
        if len(cells)!=4:raise SourceUnavailable('Search result columns changed')
        link=cells[1].find('a',href=True)
        if not link:raise SourceUnavailable('Source document link missing')
        description=cells[0].get_text('\n',strip=True)
        if not re.fullmatch(r'ASSUMED NAME(?: ABANDONMENT)?\n\d{10}',description):raise SourceUnavailable('Unexpected filing type or document number')
        rows.append({'description':description,'summary':cells[1].get_text(' ',strip=True),'url':trusted(urljoin(url,link['href']))})
    next_link=next((a for a in soup.find_all('a',href=True) if a.get_text(strip=True)=='Next'),None)
    next_url=trusted(urljoin(url,next_link['href'])) if next_link else None
    if len(rows)!=last-first+1 or bool(next_url)!=(last<total):raise SourceUnavailable('Incomplete source page or pagination')
    return rows,total,first,last,next_url

class PublicClient:
    def __init__(self,*,session=None,interval=2.0,heartbeat=None):
        self.session=session or requests.Session();self.session.headers.update({'User-Agent':AGENT})
        self.interval=max(2.0,interval);self.last=0.;self.robots=None;self.heartbeat=heartbeat or (lambda:None)
    def fetch(self,method,url,*,_redirects=0,**kwargs):
        trusted(url)
        if self.robots and not self.robots.can_fetch(AGENT,url):raise SourceUnavailable('Source robots policy disallows this request')
        wait=self.interval-(time.monotonic()-self.last)
        if wait>0:time.sleep(wait)
        self.heartbeat();self.last=time.monotonic()
        # Follow each same-origin redirect explicitly, checking robots every time.
        response=self.session.request(method,url,timeout=40,allow_redirects=False,**kwargs)
        if response.status_code in (301,302,303,307,308):
            if _redirects>=5:raise SourceUnavailable('Too many source redirects')
            target=trusted(urljoin(url,response.headers.get('Location','')))
            preserve=response.status_code in (307,308)
            return self.fetch(method if preserve else 'GET',target,_redirects=_redirects+1,**(kwargs if preserve else {}))
        if response.status_code in (401,403,429):raise SourceUnavailable('Source access restricted or rate limited; collection stopped')
        response.raise_for_status()
        if len(response.content)>5_000_000:raise SourceUnavailable('Unexpectedly large source page')
        body=response.text.lower()
        if any(marker in body for marker in ('g-recaptcha','h-captcha','cf-chl-','verify you are human','captcha challenge')):
            raise SourceUnavailable('Source requires human verification; collection stopped')
        return response
    def connect(self):
        # A 404 means no robots directives, rather than an access grant.
        try:r=self.fetch('GET',BASE+'/robots.txt')
        except requests.HTTPError as exc:
            if exc.response.status_code!=404:raise
        else:
            self.robots=RobotFileParser();self.robots.parse(r.text.splitlines())
            self.interval=max(self.interval,self.robots.crawl_delay(AGENT) or 0)
            if not self.robots.can_fetch(AGENT,SPLASH):raise SourceUnavailable('Robots policy disallows collection')
        r=self.fetch('GET',SPLASH);soup=BeautifulSoup(r.text,'html.parser')
        notice=next((p for p in soup.select('#middle > p') if p.get_text(strip=True).startswith("The County Clerk's Office, Lubbock County, Texas provides")),None)
        expected=Path(__file__).with_name('eagleweb_notice.txt').read_text().strip()
        if notice is None or soup_text(notice)!=expected:raise SourceUnavailable('Public source notice changed; review required')
        form=soup.find('form',class_='splash')
        if not form or form.get('method','').upper()!='POST':raise SourceUnavailable('Public guest entry changed')
        response=self.fetch('POST',trusted(urljoin(r.url,form['action'])),data={'guest':'true','submit':''})
        soup=BeautifulSoup(response.text,'html.parser')
        link=next((a for a in soup.find_all('a',href=True) if a.get_text(strip=True)=='Assumed Names'),None)
        if not link:raise SourceUnavailable('Public assumed-name search is unavailable')
        return self.fetch('GET',trusted(urljoin(response.url,link['href'])))
    def collect(self,start,end,directory,*,cached=None):
        date.fromisoformat(start);date.fromisoformat(end)
        if start>end:raise ValueError('Invalid recording window')
        directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
        response=self.connect();soup=BeautifulSoup(response.text,'html.parser');form=soup.find('form',attrs={'name':'docSearch'})
        if form is None:raise SourceUnavailable('Assumed-name search form changed')
        fields=[]
        for el in form.select('input[name],select[name]'):
            name=el['name']
            if name in ('RecDateIDStart','RecDateIDEnd','__search_select','AllDocumentsCB'):continue
            if el.name=='select':fields.extend((name,o.get('value','')) for o in el.select('option[selected]'))
            elif el.get('type') not in ('checkbox','submit','reset'):fields.append((name,el.get('value','')))
        options={o.get('value'):o.get_text(strip=True) for o in form.select('select[name="__search_select"] option')}
        if options!={'ASN':'ASSUMED NAME','ABN':'ASSUMED NAME ABANDONMENT'}:raise SourceUnavailable('Assumed-name document types changed')
        fields += [('RecDateIDStart',date.fromisoformat(start).strftime('%m/%d/%Y')),('RecDateIDEnd',date.fromisoformat(end).strftime('%m/%d/%Y')),('__search_select','ASN'),('__search_select','ABN')]
        response=self.fetch('POST',trusted(urljoin(response.url,form['action'])),data=fields)
        entries=[];pages=[];seen=set();total=None
        while True:
            rows,count,first,last,next_url=results(response.text,response.url)
            if total is not None and total!=count:raise SourceUnavailable('Source count changed during collection')
            total=count
            if first!=len(entries)+1:raise SourceUnavailable('Source pagination has a gap')
            for entry in rows:
                if entry['description'] in seen:raise SourceUnavailable('Duplicate source filing across pages')
                seen.add(entry['description'])
            table=BeautifulSoup(response.text,'html.parser').find(id='searchResultsTable')
            page={'url':response.url,'text':BeautifulSoup(response.text,'html.parser').get_text(' ',strip=True),'html':str(table or ''),'records':rows,'next':next_url}
            pages.append(page);entries.extend(rows)
            (directory/f'page-{len(pages)}.json').write_text(json.dumps(page))
            if not next_url:break
            if len(pages)>=30:raise SourceUnavailable('Source page limit exceeded')
            response=self.fetch('GET',next_url)
        if len(entries)!=total:raise SourceUnavailable('Incomplete source result set')
        reused=0;cached=cached or {}
        for entry in entries:
            from .detail_cache import same_summary
            previous=cached.get(entry['description'])
            if previous and same_summary(previous['entry'],entry):
                from .captured import parse_detail
                record=parse_detail(previous['capture'],entry)
                if not start<=record['recording_date']<=end:raise SourceUnavailable('Cached filing outside recording window')
                (directory/(entry['description'].rsplit('\n',1)[-1]+'.json')).write_text(json.dumps(previous['capture']))
                reused+=1;self.heartbeat();continue
            response=self.fetch('GET',entry['url']);soup=BeautifulSoup(response.text,'html.parser');presentation=soup.find(id='presentation')
            if presentation is None:raise SourceUnavailable('Full filing details unavailable')
            capture={'url':response.url,'html':str(presentation),'text':presentation.get_text(' ',strip=True),'body':soup.get_text(' ',strip=True),'captured_at':datetime.now(timezone.utc).isoformat()}
            from .captured import parse_detail
            record=parse_detail(capture,entry)
            if not start<=record['recording_date']<=end:raise SourceUnavailable('Source filing falls outside the requested recording window')
            (directory/(entry['description'].rsplit('\n',1)[-1]+'.json')).write_text(json.dumps(capture))
        # Recheck every summary page so records moving during pagination fail closed.
        for page in pages:
            response=self.fetch('GET',page['url']);rows,count,*_=results(response.text,response.url)
            if count!=total or rows!=page['records']:raise SourceUnavailable('Source results changed before reconciliation')
        index={'url':pages[0]['url'],'text':pages[0]['text'],'reported_total':total,'pages':len(pages),'records':entries,'search_start':start,'search_end':end,'collection_method':'public_guest_html','unattended':True,'detail_pages_retrieved':len(entries)-reused,'archived_details_reused':reused}
        (directory/'index.json').write_text(json.dumps(index))
        return index
