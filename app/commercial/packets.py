"""Presentation sheets followed by readable, source-backed supporting schedules."""
from io import BytesIO
from datetime import datetime
from zoneinfo import ZoneInfo
from html import escape
from .equity import generate_equity
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import letter, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import (BaseDocTemplate, PageTemplate, Frame, Paragraph, Spacer,
                               Table, TableStyle, PageBreak, NextPageTemplate, Flowable, KeepInFrame)

INK=colors.HexColor('#203b30')
GREEN=colors.HexColor('#37664f')
MUTED=colors.HexColor('#66766d')
PALE=colors.HexColor('#f0f5f1')
LINE=colors.HexColor('#dce5de')


PACKET_LABELS={
    'cap_rate':'Base cap rate','loaded_cap_rate':'Loaded cap rate','vacancy_rate':'Vacancy',
    'expense_rate':'Expenses','operating_expense_rate':'Operating expenses',
    'departmental_expense_rate':'Departmental expenses','management_rate':'Management',
    'reserve_rate':'Reserves','franchise_rate':'Franchise','insurance_rate':'Insurance',
    'tax_rate':'Tax rate','other_income_rate':'Other income','bad_debt_rate':'Bad debt',
    'loss_to_lease_rate':'Loss to lease','rent':'Rent','rent_per_unit':'Rent per unit',
    'primary_rent':'Primary rent','secondary_rent':'Secondary rent','office_rent':'Office rent',
    'tertiary_rent':'Tertiary rent','quaternary_rent':'Additional rent',
    'room_revenue_2022':'Room revenue - 2022','room_revenue_2025':'Room revenue - 2025',
    'room_revenue_2026':'Room revenue - 2026',
}


def display(value,field=''):
    if value is None:return 'Not supplied'
    if isinstance(value,(int,float)):
        if field.endswith('_rate'):return f'{value*100:,.2f}%'
        return f'{value:,.2f}' if 'rent' in field or field=='unit_value' else f'{value:,.0f}'
    return str(value)


def currency(value):
    if not isinstance(value,(int,float)):return 'Unavailable'
    return f'(${abs(value):,.0f})' if value<0 else f'${value:,.0f}'


class SectionMarker(Flowable):
    def __init__(self,account,section,key,root=None):
        Flowable.__init__(self);self.account=account;self.section=section;self.key=key;self.root=root
        self.width=self.height=0
    def draw(self):
        c=self.canv;c._packet_account=self.account;c._packet_section=self.section
        c.bookmarkPage(self.key)
        if self.root:c.addOutlineEntry(self.root,self.key,0,False)
        else:c.addOutlineEntry(self.section,self.key,1,False)


class ValueCards(Flowable):
    def __init__(self,cards,width):
        Flowable.__init__(self);self.cards=cards;self.width=width;self.height=77
    def draw(self):
        c=self.canv;parts=[.46,.27,.27] if len(self.cards)==3 else [1/len(self.cards)]*len(self.cards)
        c.setFillColor(PALE);c.roundRect(0,0,self.width,self.height,7,fill=1,stroke=0)
        x=0
        for i,((label,value),part) in enumerate(zip(self.cards,parts)):
            c.setFillColor(MUTED);c.setFont('Helvetica',8);c.drawString(x+14,56,label.upper())
            size=25 if i==0 else 17
            while c.stringWidth(value,'Helvetica-Bold',size)>self.width*part-24 and size>10:size-=1
            c.setFillColor(INK);c.setFont('Helvetica-Bold',size);c.drawString(x+14,23,value)
            x+=self.width*part
            if i<len(self.cards)-1:
                c.setStrokeColor(LINE);c.line(x,15,x,self.height-15)


class PacketBuilder:
    def __init__(self):
        self.story=[]
        self.styles={
            'body':ParagraphStyle('body',fontName='Helvetica',fontSize=9,leading=12,textColor=INK,spaceAfter=0),
            'small':ParagraphStyle('small',fontName='Helvetica',fontSize=7.8,leading=10.5,textColor=MUTED),
            'eyebrow':ParagraphStyle('eyebrow',fontName='Helvetica-Bold',fontSize=8,leading=11,textColor=GREEN,spaceAfter=6),
            'title':ParagraphStyle('title',fontName='Helvetica-Bold',fontSize=25,leading=29,textColor=INK,spaceAfter=6),
            'name':ParagraphStyle('name',fontName='Helvetica-Bold',fontSize=12,leading=15,textColor=INK,spaceAfter=3),
            'section':ParagraphStyle('section',fontName='Helvetica-Bold',fontSize=11,leading=15,textColor=GREEN,spaceBefore=12,spaceAfter=6,keepWithNext=True),
            'cell':ParagraphStyle('cell',fontName='Helvetica',fontSize=8.5,leading=11,textColor=INK),
            'number':ParagraphStyle('number',fontName='Helvetica',fontSize=9,leading=11,textColor=INK,alignment=TA_RIGHT),
            'bold':ParagraphStyle('bold',fontName='Helvetica-Bold',fontSize=9,leading=12,textColor=INK),
            'head':ParagraphStyle('head',fontName='Helvetica-Bold',fontSize=7.5,leading=10,textColor=MUTED),
            'head_number':ParagraphStyle('head_number',fontName='Helvetica-Bold',fontSize=7.5,leading=10,textColor=MUTED,alignment=TA_RIGHT),
            'dense':ParagraphStyle('dense',fontName='Helvetica',fontSize=8,leading=10,textColor=INK),
            'dense_number':ParagraphStyle('dense_number',fontName='Helvetica',fontSize=8.5,leading=10.5,textColor=INK,alignment=TA_RIGHT),
        }
    def p(self,text,style='body'):
        return Paragraph(escape(str(text).translate(str.maketrans({chr(c):'-' for c in range(0x2010,0x2016)}))).replace('\n','<br/>'),self.styles[style])
    def add(self,*items):self.story.extend(items)
    def heading(self,text):self.add(self.p(text,'section'))
    def table(self,rows,widths,numeric=(),highlights=(),subject_column=None,dense=False):
        cells=[]
        for row_index,row in enumerate(rows):
            cells.append([self.p(value,'head_number' if row_index==0 and col in numeric else 'head' if row_index==0 else 'dense_number' if dense and col in numeric else 'number' if col in numeric else 'dense' if dense else 'cell') for col,value in enumerate(row)])
        t=Table(cells,colWidths=widths,repeatRows=1,hAlign='LEFT')
        commands=[('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),8),('RIGHTPADDING',(0,0),(-1,-1),8),('TOPPADDING',(0,0),(-1,-1),3 if dense else 4),('BOTTOMPADDING',(0,0),(-1,-1),3 if dense else 4),('LINEBELOW',(0,0),(-1,0),.7,LINE),('BACKGROUND',(0,0),(-1,0),PALE),('LINEBELOW',(0,1),(-1,-1),.35,LINE)]
        if subject_column is not None:commands.append(('BACKGROUND',(subject_column,0),(subject_column,-1),colors.HexColor('#e7f0e8')))
        for row in highlights:
            commands.extend([('BACKGROUND',(0,row),(-1,row),PALE),('LINEABOVE',(0,row),(-1,row),.6,LINE)])
            for col in range(len(cells[row])):
                text=rows[row][col]
                cells[row][col]=self.p(text,'bold' if col not in numeric else 'number')
        t._cellvalues=cells
        t.setStyle(TableStyle(commands));return t
    def page(self,account,section,key,wide=False,root=None,first=False):
        if not first:self.add(NextPageTemplate('wide' if wide else 'portrait'),PageBreak())
        self.add(SectionMarker(account,section,key,root))
    def header(self,detail,title,kicker='2026 COMMERCIAL APPRAISAL / ARB SUPPORT',wide=False):
        v=detail['values'];name=v.get('name') or v.get('owner') or detail['label']
        if wide:
            self.add(self.p(kicker,'eyebrow'),self.p(title,'name'),self.p(detail['account']+'  |  '+str(name),'small'),Spacer(1,7))
        else:self.add(self.p(kicker,'eyebrow'),self.p(title,'title'),self.p(detail['account']+'  |  '+str(name),'name'),self.p(v.get('address') or 'Address not supplied in model','small'),Spacer(1,12))
    def facts(self,detail):
        v=detail['values'];pairs=[('MODEL',detail['label']),('CATEGORY',v.get('category') or v.get('classification') or 'Unclassified')]
        for label,key in [('YEAR BUILT','year_built'),('BUILDING AREA','area_sq_ft'),('LEASABLE AREA','leasable_area_sq_ft'),('UNITS','units'),('ROOMS','rooms')]:
            if v.get(key) is not None:
                pairs.append((label,(str(int(v[key])) if key=='year_built' and isinstance(v[key],(int,float)) else display(v[key]))+(' sq ft' if 'area' in key else '')))
        rows=[]
        for start in range(0,len(pairs),2):
            row=[]
            for label,value in pairs[start:start+2]:row.extend([self.p(label,'head'),self.p(value,'cell')])
            while len(row)<4:row.append('')
            rows.append(row)
        t=Table(rows,colWidths=[75,186,75,186],hAlign='LEFT');t.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),0),('RIGHTPADDING',(0,0),(-1,-1),8),('TOPPADDING',(0,0),(-1,-1),4),('BOTTOMPADDING',(0,0),(-1,-1),4)]));return t
    def assumptions(self,detail):
        v=detail['values'];keys=['cap_rate','vacancy_rate','expense_rate','operating_expense_rate','departmental_expense_rate','management_rate','reserve_rate','franchise_rate','insurance_rate','tax_rate','other_income_rate','bad_debt_rate','loss_to_lease_rate','rent','rent_per_unit','primary_rent','secondary_rent','office_rent','tertiary_rent','quaternary_rent']
        fields={f['key']:f for f in detail['fields']}
        cells=[]
        for key in keys:
            if key not in fields or v.get(key) is None:continue
            f=fields[key]
            label=PACKET_LABELS.get(key,f['label'])
            unit=rent_basis(detail['model'],key)
            cells.append(self.p(label+'\n'+(currency_unit(v[key])+unit.removeprefix('$') if unit else display(v[key],key)),'small'))
        if not cells:return None
        rows=[cells[i:i+3]+['']*(3-len(cells[i:i+3])) for i in range(0,len(cells),3)]
        t=Table(rows,colWidths=[174]*3,hAlign='LEFT');t.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),8),('RIGHTPADDING',(0,0),(-1,-1),8),('TOPPADDING',(0,0),(-1,-1),5),('BOTTOMPADDING',(0,0),(-1,-1),5),('BOX',(0,0),(-1,-1),.5,LINE)]));return t


def rent_basis(model,key):
    if key not in ('rent','rent_per_unit','primary_rent','secondary_rent','office_rent','tertiary_rent','quaternary_rent') and not (key.startswith('mix_') and key.endswith('_monthly_rent')):return ''
    if model=='apartments' or key=='rent_per_unit':return '$ / unit / month'
    return '$ / sq ft / month' if model=='miniwarehouse_storage' else '$ / sq ft / year'


def build_packet(store,ids,comparison_ids=None,notes=''):
    if not 1<=len(ids)<=100:raise ValueError('Select between 1 and 100 model rows per packet batch')
    b=PacketBuilder()
    for index,record_id in enumerate(ids):
        detail=store.detail(record_id);v=detail['values'];account=detail['account'];equity=generate_equity(store,record_id)
        final=v.get('total_value') if v.get('total_value') is not None else v.get('indicated_value')
        cost=detail['model']=='cost';basis=equity['unit'];subject=equity['subject'];peers=[subject]+equity['comparables']
        b.page(account,'Cost summary' if cost else 'Pro Forma',f'{index}-main',root=account+' / '+detail['label'],first=index==0)
        main_start=len(b.story)
        b.header(detail,'Cost summary' if cost else 'Pro Forma')
        second=('Land',currency(v.get('land_value'))) if cost else ('Net operating income',currency(v.get('noi')))
        third=('Improvements',currency(v.get('improvement_value'))) if cost else ('Loaded cap rate' if v.get('loaded_cap_rate') is not None else 'Base cap rate',display(v.get('loaded_cap_rate') if v.get('loaded_cap_rate') is not None else v.get('cap_rate'),'cap_rate'))
        b.add(ValueCards([('Final model value' if v.get('total_value') is not None else 'Income indication' if not cost else 'Model value',currency(final)),second,third],522),Spacer(1,10),b.facts(detail))
        b.heading('Value reconciliation' if cost else 'Annual income & value')
        keys=['land_value','improvement_value','total_value','area_sq_ft','unit_value'] if cost else ['projected_gross_rent','room_revenue_2022','room_revenue_2025','room_revenue_2026','vacancy_loss','effective_gross_rent','other_income','other_income_amount','bad_debt_amount','effective_gross_income','total_expenses','reserve_amount','noi','cap_rate','tax_rate','loaded_cap_rate','indicated_value','other_improvements','other_land','other_adjustments','land_adjustment','other_component_value','other_component_land_value','total_value']
        fields={f['key']:f for f in detail['fields']};rows=[['VALUE COMPONENT' if cost else 'INCOME / EXPENSE / CAPITALIZATION','AMOUNT / RATE']];highlights=[]
        adjustments={'other_improvements','other_land','other_adjustments','land_adjustment','other_component_value','other_component_land_value'}
        for key in keys:
            f=fields.get(key)
            if not f or f['value'] is None and not f['error']:continue
            if key in adjustments and f['value']==0 and not f['error']:continue
            value=f['value'];label=PACKET_LABELS.get(key,f['label'])
            if f['error']:text='Unavailable'
            elif key=='unit_value':text=currency_unit(value)
            elif key.endswith('_rate') or key=='area_sq_ft':text=display(value,key)
            else:
                if key in ('vacancy_loss','bad_debt_amount','total_expenses','reserve_amount') and isinstance(value,(int,float)):value=-abs(value)
                text=currency(value)
            if key in ('noi','total_value','effective_gross_income'):highlights.append(len(rows))
            rows.append([label,text])
        b.add(b.table(rows,[365,157],numeric=(1,),highlights=highlights))
        assumptions=b.assumptions(detail)
        if assumptions:
            b.heading('Key assumptions');b.add(assumptions)
        if cost:
            b.heading('Presentation guide');b.add(b.p('Start with the land and improvement values, reconcile them to the final model value, then use the equity sheet to compare value per square foot. The source price per square foot is retained separately; it is not substituted for total value divided by area.','small'))
        if not cost:
            if detail['model']=='hotels':b.add(b.p('Room revenues are shown by source year. The hotel model retains its source revenue averaging and capitalization method.','small'))
            explanation='Annual income less expenses produces NOI. The source capitalization method and reconciliation produce the final model value. Deductions appear in parentheses.'
            if isinstance(final,(int,float)) and isinstance(v.get('indicated_value'),(int,float)) and abs(final-v['indicated_value'])>.5:
                explanation+=' Final value differs from income indication; see the reconciliation schedule.'
            b.add(Spacer(1,8),b.p(explanation,'small'))
        if detail['errors']:b.add(b.p(f"{len(detail['errors'])} source calculation exceptions. Unavailable figures are not treated as zero; see Source notes.",'small'))

        b.story[main_start:]=[KeepInFrame(522,700,b.story[main_start:],mode='shrink',hAlign='LEFT',vAlign='TOP')]
        b.page(account,'Equity comparison',f'{index}-equity');equity_start=len(b.story);b.header(detail,'Equity comparison')
        subject_unit=subject['unit_value'];median=equity['median_unit_value']
        delta=(subject_unit/median-1)*100 if isinstance(subject_unit,(int,float)) and isinstance(median,(int,float)) and median>0 else None
        b.add(ValueCards([('Subject / '+basis,currency_unit(subject_unit)),('Peer median / '+basis,currency_unit(median)),('Subject vs. median',('0.0%' if abs(delta)<.05 else f'{delta:+.1f}%') if delta is not None else 'Unavailable')],522))
        b.add(Spacer(1,10),b.p('Comparison basis: value per '+basis+'. '+str(len(equity['comparables']))+' distinct peer accounts shown. These are model comparisons, not sales.','small'))
        b.heading('Subject and model peers')
        rows=[['ACCOUNT / PROPERTY',basis.upper(),'BUILT','MODEL VALUE','VALUE / '+basis.upper()]]
        for n,r in enumerate(peers):
            cv=r['values'];match='' if n==0 else '\n'+('Exact category' if r['match_tier']==0 else 'Same category - broader fit' if r['match_tier']==1 else 'Related use - review' if r['match_tier']==2 else 'Broader model peer - review')
            rows.append([('SUBJECT' if n==0 else 'COMP '+str(n))+' / '+r['account']+'\n'+str(cv.get('name') or r['label'])+'\n'+str(cv.get('address') or 'Address not supplied')+match,display(r['size']),str(int(cv['year_built'])) if isinstance(cv.get('year_built'),(int,float)) else 'Not supplied',currency(r['comparison_value']),currency_unit(r['unit_value'])])
        b.add(b.table(rows,[202,64,47,106,103],numeric=(1,2,3,4),highlights=(1,),dense=True))
        b.heading('Presentation guide')
        b.add(b.p('Establish the common size basis, compare subject and median unit values, then review the physical, rent and operating evidence. The median includes all displayed peers.','small'))
        if equity['missing_reason']:b.add(Spacer(1,7),b.p(equity['missing_reason'],'small'))
        b.add(Spacer(1,7),b.p('Unadjusted median x subject size: '+currency(equity['unadjusted_indication'])+'. No location, condition or dollar adjustments have been inferred.','small'))

        b.story[equity_start:]=[KeepInFrame(522,700,b.story[equity_start:],mode='shrink',hAlign='LEFT',vAlign='TOP')]

        # Wider schedules retain all supplied peer data without a seven-column squeeze.
        groups=[('Property & rent data',('Property characteristics','Rent and space components')),
                ('Operating & income data',('Operating assumptions','Annual income and expenses')),
                ('Value reconciliation & source notes',('Value reconciliation',))]
        for group_index,(title,titles) in enumerate(groups):
            if group_index!=2 and not any(section['title'] in titles for section in equity['sections']):continue
            b.page(account,title,f'{index}-support-{group_index}',wide=True)
            b.header(detail,title,'SUPPORTING SCHEDULE / '+account,wide=True)
            b.add(b.p('Shaded column: subject. Rent basis is identified below. Not supplied = missing data; unavailable = calculation exception.','small'),Spacer(1,5))
            for section in equity['sections']:
                if section['title'] not in titles:continue
                b.heading(section['title'])
                rows=[['MODEL INPUT / RESULT']+['SUBJECT\n'+r['account'] if n==0 else 'COMP '+str(n)+'\n'+r['account'] for n,r in enumerate(peers)]]
                for row in section['rows']:
                    label=row['label']
                    if rent_basis(detail['model'],row['key']):
                        label=label.replace(' (source basis)','')+'\n'+rent_basis(detail['model'],row['key'])
                    cells=[]
                    for cell in row['cells']:
                        value=cell['value']
                        if cell['error']:text='Unavailable'
                        elif value is None:text='Not supplied'
                        elif row['format']=='rate':text=display(value,'cap_rate')
                        elif row['format']=='money':text=currency_unit(value) if rent_basis(detail['model'],row['key']) else currency(value)
                        elif row['format']=='text':text=str(value)
                        else:text=display(value)
                        cells.append(text)
                    rows.append([label]+cells)
                b.add(b.table(rows,[174]+[546/len(peers)]*len(peers),numeric=tuple(range(1,len(peers)+1)) if all(row['format']!='text' for row in section['rows']) else (),subject_column=1,dense=True))
            if group_index==0:
                mix=[]
                for key,value in v.items():
                    if key.startswith('mix_') and key.endswith('_units') and isinstance(value,(int,float)) and value>0:
                        prefix=key.removesuffix('_units');label=fields.get(key,{}).get('label',f'Configuration {int(prefix.split("_")[1])}').removesuffix(' - Units')
                        mix.append([label,display(value),currency_unit(v.get(prefix+'_monthly_rent')),display(v.get(prefix+'_unit_area_sq_ft'))])
                if mix:
                    b.heading('Subject apartment unit mix');b.add(b.table([['CONFIGURATION','UNITS','MONTHLY RENT / UNIT','UNIT AREA (SQ FT)']]+mix,[315,80,175,150],numeric=(1,2,3)))
                b.heading('Peer selection');b.add(b.p(equity['rules'],'small'))
            if group_index==2:
                b.heading('Source references')
                refs=[['ACCOUNT','MODEL WORKBOOK','WORKSHEET / ROW']]
                for r in peers:refs.append([r['account'],r['source_file'],r['source_sheet']+' / '+str(r['source_row'])+(' (new property)' if r.get('new_property') else '')])
                b.add(b.table(refs,[105,350,265],dense=True))
                exceptions={}
                for r in peers:
                    for field,error in r.get('errors',{}).items():exceptions.setdefault((r['account'],str(error)),[]).append(field.replace('_',' '))
                if exceptions:
                    b.heading('Calculation exceptions')
                    for (acct,error),names in exceptions.items():b.add(b.p(acct+' / '+', '.join(names)+': '+error,'small'),Spacer(1,4))
                b.add(Spacer(1,8),b.p(equity['explanation']+' Displayed figures are rounded; calculations retain source precision.','small'))
                if notes.strip():b.heading('Appraiser notes');b.add(b.p(notes))
    stream=BytesIO();prepared=datetime.now(ZoneInfo('America/Chicago')).strftime('%b %d, %Y')
    def page_furniture(canvas,doc):
        width,height=canvas._pagesize;canvas.saveState();canvas.setStrokeColor(LINE);canvas.line(36,height-27,width-36,height-27)
        canvas.setFont('Helvetica-Bold',7);canvas.setFillColor(GREEN);canvas.drawString(36,height-20,'RENDITION PILOT  /  COMMERCIAL')
        canvas.setFillColor(MUTED);canvas.setFont('Helvetica',7);canvas.drawRightString(width-36,height-20,'Prepared '+prepared);canvas.restoreState()
    class PacketDocument(BaseDocTemplate):
        def afterPage(self):
            c=self.canv;w,h=c._pagesize;c.saveState();c.setStrokeColor(LINE);c.line(36,35,w-36,35);c.setFont('Helvetica',7);c.setFillColor(MUTED);c.drawString(36,23,getattr(c,'_packet_account','')+'  /  '+getattr(c,'_packet_section','Commercial appraisal'));c.drawRightString(w-36,23,'Page '+str(self.page));c.restoreState()
    document=PacketDocument(stream,pagesize=letter,title='Commercial ARB presentation packet',author='Rendition Pilot')
    document.addPageTemplates([PageTemplate(id='portrait',pagesize=letter,frames=[Frame(45,48,522,700,leftPadding=0,rightPadding=0,topPadding=0,bottomPadding=0)],onPage=page_furniture),PageTemplate(id='wide',pagesize=landscape(letter),frames=[Frame(36,48,720,520,leftPadding=0,rightPadding=0,topPadding=0,bottomPadding=0)],onPage=page_furniture)])
    document.build(b.story)
    return stream.getvalue()


def currency_unit(value):
    if not isinstance(value,(int,float)):return 'Unavailable'
    return f'(${abs(value):,.2f})' if value<0 else f'${value:,.2f}'
