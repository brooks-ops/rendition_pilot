"""Deterministic EagleWeb printable-results extraction. No model guesses."""
from __future__ import annotations
import bisect,hashlib,re
from datetime import datetime
from urllib.parse import urlparse
import fitz

MAX_BYTES=25*1024*1024
MAX_PAGES=250
PARSER_VERSION='eagleweb-print-v1'
class ImportError(ValueError):pass

def clean(value):return re.sub(r'\s+',' ',value or '').strip()
def date_value(value):
    m=re.search(r'\b(\d{1,2}/\d{1,2}/\d{4})\b',value or '')
    if not m:return None
    try:return datetime.strptime(m[1],'%m/%d/%Y').date().isoformat()
    except ValueError:return None

def parse_pdf(data:bytes,*,source_url:str):
    if len(data)>MAX_BYTES:raise ImportError('Choose a PDF smaller than 25 MB.')
    if b'%PDF-' not in data[:1024]:raise ImportError('Choose an EagleWeb printable search-results PDF.')
    try:doc=fitz.open(stream=data,filetype='pdf')
    except Exception as exc:raise ImportError('The PDF could not be opened. Export it again from EagleWeb.') from exc
    try:
        if doc.needs_pass:raise ImportError('Password-protected PDFs cannot be imported. Supply an unlocked printable-results PDF.')
        if not 1<=len(doc)<=MAX_PAGES:raise ImportError(f'Use a PDF with 1–{MAX_PAGES} printed pages.')
        texts=[];offsets=[];references={};clipped=set();offset=0;printed_pages=[]
        host=urlparse(source_url).hostname
        verified_source=False
        for page_no,page in enumerate(doc,1):
            text=page.get_text(sort=False);offsets.append(offset)
            source_urls=re.findall(r'https?://[^\s<>]+',text)+[l.get('uri','') for l in page.get_links()]
            record_hosts={urlparse(u).hostname for u in source_urls if '/recorder/' in u}
            if record_hosts-{host}:raise ImportError('This PDF references a different county recorder. Use the source configured for your district.')
            verified_source=verified_source or host in record_hosts
            # The physical print page count and the web search-result count are different.
            printed_pages.extend((int(a),int(b)) for a,b in re.findall(r'(?m)^\s*(\d+)\s*/\s*(\d+)\s*$',text))
            links=[l for l in page.get_links() if urlparse(l.get('uri','')).scheme=='https' and urlparse(l.get('uri','')).hostname==host and urlparse(l.get('uri','')).path.endswith('/viewDoc.jsp')]
            links.sort(key=lambda l:l['from'].y0)
            for link_index,link in enumerate(links):
                box=link['from'];row_end=links[link_index+1]['from'].y0 if link_index+1<len(links) else page.rect.height-25
                ids=[w[4] for w in page.get_text('words') if re.fullmatch(r'[A-Z0-9][A-Z0-9./-]{3,49}',w[4]) and any(c.isdigit() for c in w[4]) and w[0]<box.x0 and box.y0<=w[1]<row_end]
                for number in ids:
                    references[number]=link['uri']
                    if any(w[4].startswith('Owner') and w[0]>page.rect.width*.75 and box.y0<=w[1]<row_end for w in page.get_text('words')):clipped.add(number)
            # Exclude print headers/footers from field values, while retaining original PDF bytes.
            lines=[l for l in text.splitlines() if not re.match(r'^\s*(?:https?://|\d+/\d+\s*$|\d{1,2}/\d{1,2}/\d{2},\s+\d|Search Results\s*$|Return to Search Results\s*$|Description\s*$|Summary\s*$)',l)]
            cleaned='\n'.join(lines)+'\n';texts.append(cleaned);offset+=len(cleaned)
        text=''.join(texts)
        if len(clean(text))<30:raise ImportError('No searchable filing text was found. Use EagleWeb’s printable search-results PDF; scanned images need OCR first.')
        heads=list(re.finditer(r'(?P<type>ASSUMED\s+NAME(?:\s+ABANDONMENT)?)\s+(?P<number>[A-Z0-9][A-Z0-9./-]{3,49})\s+(?=Recording\s*Date\s*:)',text,re.I))
        if not heads:raise ImportError('No EagleWeb assumed-name records were recognized. No filings were imported; check the printable-results format.')
        records=[];rejected=[];seen={};conflicts=[]
        for i,head in enumerate(heads):
            end=heads[i+1].start() if i+1<len(heads) else len(text);block=text[head.end():end]
            block=re.split(r'\d[\d,]*\s+items found,',block,flags=re.I)[0]
            def value(pattern):
                m=re.search(pattern,block,re.I|re.S);return clean(m[1]) if m else None
            recording=value(r'Recording\s*Date\s*:\s*(.*?)(?=Assumed\s*Name\s*:|$)')
            name=value(r'Assumed\s*Name\s*:\s*(.*?)(?=Owner(?:\(s\)|s)?\s*:|Expires\s*:|$)')
            owner=value(r'Owner(?:\(s\)|s)?\s*:\s*(.*?)(?=Expires\s*:|$)')
            expiry=value(r'Expires\s*:\s*(.*?)(?=\n|$)')
            number=head['number'].upper();kind=clean(head['type']).upper();recorded=date_value(recording)
            page_first=bisect.bisect_right(offsets,head.start());page_last=bisect.bisect_right(offsets,max(head.start(),end-1))
            if not recorded or not name:
                rejected.append({'document_number':number,'reason':'Recording date or assumed name is missing/invalid.','source_pages':list(range(page_first,page_last+1))});continue
            is_clipped=number in clipped or bool(owner and re.search(r'(?:\.{3}|…)$',owner))
            warnings=[]
            if is_clipped:warnings.append('Owner text is clipped or unverified; open the individual filing for the full name.')
            expiration=date_value(expiry)
            if expiry and not expiration:warnings.append('Expiration date could not be read; original text retained.')
            record={'document_number':number,'filing_type':kind,'recording_date':recorded,'recording_date_raw':recording,'assumed_name':name,'expiration_date':expiration,'owner_name':None if is_clipped else owner,'owner_text':owner,'owner_name_complete':bool(owner and not is_clipped),'business_address':None,'mailing_address':None,'additional_parties':[], 'source_pages':list(range(page_first,page_last+1)),'document_reference':references.get(number),'raw_text':text[head.start():end].strip(),'warnings':warnings}
            key=(number,kind)
            if key in seen:
                if any(seen[key].get(k)!=record.get(k) for k in ('recording_date','assumed_name','expiration_date','owner_text')):conflicts.append(number)
                continue
            seen[key]=record;records.append(record)
        windows=[];totals=[]
        for total,a,b in re.findall(r'([\d,]+)\s+items found,\s*displaying\s+(\d+)\s+to\s+(\d+)',text,re.I):
            totals.append(int(total.replace(',','')));windows.append((int(a),int(b)))
        windows=sorted(set(windows));total=max(totals) if totals else None
        # Union intervals, never confuse the eight printed pages with the 13 web result pages.
        covered=0;end=0;gap=False
        for a,b in windows:
            if a>end+1:gap=True
            covered+=max(0,b-max(a,end+1)+1);end=max(end,b)
        incomplete=bool(total is not None and (gap or not windows or windows[0][0]!=1 or end<total or len(records)<covered))
        coverage='INCOMPLETE' if incomplete or rejected or conflicts else 'COMPLETE' if total is not None and len(records)==total and not gap else 'UNKNOWN'
        warnings=[]
        if not verified_source:
            coverage='UNKNOWN';warnings.append('The PDF does not contain a verifiable county recorder URL. Verify its county before importing.')
        if coverage=='INCOMPLETE':warnings.append(f'Incomplete search results: this PDF contains {len(records):,} unique filings; EagleWeb reports {total:,} total results.' if total is not None else 'Some filing rows were rejected or conflict. This import is incomplete.')
        if coverage=='UNKNOWN':warnings.append('Search-result completeness cannot be verified. Import only the extracted filings; do not treat this as a complete date-range retrieval.')
        if clipped:warnings.append(f'{sum(r["document_number"] in clipped for r in records)} owner entries appear clipped. Partial owner text is preserved but excluded from name matching.')
        if rejected:warnings.append(f'{len(rejected)} unreadable filing rows were excluded. Review the source PDF.')
        if conflicts:raise ImportError('Conflicting versions of document '+', '.join(sorted(set(conflicts)))+' occur in this PDF. Split or correct the export before importing.')
        if printed_pages and (max(b for a,b in printed_pages)!=len(doc) or len({a for a,b in printed_pages})!=len(doc)):
            coverage='INCOMPLETE';warnings.append('Printed PDF pages may be missing. Re-export all printed pages.')
        search_dates=[]
        for month,day,year in re.findall(r'\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2})\s+\d{2}:\d{2}:\d{2}\s+\w+\s+(\d{4})',text):
            try:search_dates.append(datetime.strptime(f'{month} {day} {year}','%b %d %Y').date().isoformat())
            except ValueError:pass
        return {'sha256':hashlib.sha256(data).hexdigest(),'parser_version':PARSER_VERSION,'page_count':len(doc),'records':records,'rejected_records':rejected,'record_count':len(records),'reported_total':total,'search_windows':[list(x) for x in windows],'search_start':search_dates[0] if search_dates else None,'search_end':search_dates[1] if len(search_dates)>1 else None,'coverage':coverage,'warnings':warnings,'notice':'A DBA filing does not prove a business opened. An abandonment does not prove it closed.'}
    finally:doc.close()
