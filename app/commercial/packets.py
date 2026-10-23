from io import BytesIO
from datetime import datetime
from zoneinfo import ZoneInfo
from html import escape
from .equity import generate_equity
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, Flowable


def display(value,field=''):
    if value is None:return 'Not supplied'
    if isinstance(value,(int,float)):
        if field.endswith('_rate'):return f'{value*100:,.2f}%'
        return f'{value:,.2f}' if field in ('rent','primary_rent','secondary_rent','tertiary_rent','quaternary_rent','unit_value','rent_per_unit') else f'{value:,.0f}'
    return str(value)


def currency(value):
    if not isinstance(value,(int,float)):return 'Unavailable'
    return f'(${abs(value):,.0f})' if value<0 else f'${value:,.0f}'


def build_packet(store,ids,comparison_ids=None,notes=''):
    if not 1<=len(ids)<=100:raise ValueError('Select between 1 and 100 model rows per packet batch')
    stream=BytesIO();styles=getSampleStyleSheet()
    styles.add(ParagraphStyle(name='SmallSource',fontName='Helvetica',fontSize=8,leading=11,textColor=colors.HexColor('#566478')))
    styles.add(ParagraphStyle(name='TableCompact',fontName='Helvetica',fontSize=8,leading=9))
    styles.add(ParagraphStyle(name='TableDense',fontName='Helvetica',fontSize=7,leading=9))
    styles['Title'].fontName='Helvetica-Bold';styles['Title'].fontSize=24;styles['Title'].alignment=0
    styles['BodyText'].fontSize=9;styles['BodyText'].leading=12
    styles['Heading2'].textColor=colors.HexColor('#256650')
    story=[]
    def p(text,style='BodyText'):return Paragraph(escape(str(text)).replace('\n','<br/>'),styles[style])
    def table(rows,widths,compact=False,dense=False):
        t=Table([[p(cell,'TableDense' if dense else 'TableCompact' if compact else 'BodyText') for cell in row] for row in rows],colWidths=widths,repeatRows=1,hAlign='LEFT')
        t.setStyle(TableStyle([('LEFTPADDING',(0,0),(-1,-1),3 if compact else 6),('RIGHTPADDING',(0,0),(-1,-1),3 if compact else 6),('BACKGROUND',(0,0),(-1,0),colors.HexColor('#e9eef3')),('VALIGN',(0,0),(-1,-1),'TOP'),('BOTTOMPADDING',(0,0),(-1,-1),3 if compact else 5),('TOPPADDING',(0,0),(-1,-1),3 if compact else 5),('LINEBELOW',(0,0),(-1,0),0.7,colors.HexColor('#94a3b8')),('LINEBELOW',(0,1),(-1,-1),0.3,colors.HexColor('#e2e8f0'))]))
        return t
    for i,record_id in enumerate(ids):
        detail=store.detail(record_id);v=detail['values']
        if i:story.append(PageBreak())
        class AccountMarker(Flowable):
            def draw(self):self.canv._packet_account=self.account
        marker=AccountMarker();marker.account=detail['account'];marker.width=marker.height=0
        story.extend([marker,p('2026 COMMERCIAL APPRAISAL','SmallSource'),Spacer(1,8),p(v.get('account') or detail['account'],'Title'),p(v.get('name') or detail['label'],'Heading2'),p(v.get('address') or 'Address not supplied in model'),Spacer(1,10)])
        source_row=f'workspace row {detail["source_row"]} (new property)' if detail.get('new_property') else f'row {detail["source_row"]}'
        story.extend([p(detail['label']+' / '+str(v.get('category') or v.get('classification') or 'Unclassified'),'SmallSource'),p(f"{detail['source_file']} / {detail['source_sheet']} / {source_row}",'SmallSource')])
        story.extend([Spacer(1,14),table([['FINAL MODEL VALUE','AREA / UNITS / ROOMS'],[currency(v.get('total_value') if v.get('total_value') is not None else v.get('indicated_value')),display(v.get('rooms') if detail['model']=='hotels' else v.get('units') if detail['model']=='apartments' else v.get('area_sq_ft'))]],[340,182])])
        story.extend([Spacer(1,14),p('Cost summary' if detail['model']=='cost' else 'Pro Forma','Heading2')])
        keys=['land_value','improvement_value','total_value','area_sq_ft','unit_value'] if detail['model']=='cost' else ['projected_gross_rent','vacancy_loss','effective_gross_rent','other_income_amount','bad_debt_amount','effective_gross_income','total_expenses','reserve_amount','noi','cap_rate','tax_rate','loaded_cap_rate','indicated_value','other_improvements','other_land','other_adjustments','land_adjustment','other_component_value','other_component_land_value','total_value']
        assumption_keys=['rent','primary_rent','rent_per_unit','vacancy_rate','expense_rate','reserves_rate','cap_rate','tax_rate']
        assumptions=[f for f in detail['fields'] if f['key'] in assumption_keys and f['value'] is not None]
        if assumptions:story.extend([Spacer(1,8),p(' / '.join(f['label']+': '+display(f['value'],f['key']) for f in assumptions),'SmallSource')])
        by_key={f['key']:f for f in detail['fields']}
        fields=[by_key[k] for k in keys if k in by_key and (by_key[k]['value'] is not None or by_key[k]['error'])]
        story.append(p('Annual income flows to net operating income, then the source capitalization rate produces an indicated value. Final model values retain source adjustments and overrides.' if detail['model']!='cost' else 'Land and improvements reconcile to the model total.','SmallSource'))
        story.append(table([['Income and valuation bridge','Amount / rate']]+[[f['label'],('Unavailable - '+f['error']) if f['error'] else (display(f['value'],f['key']) if f['key'].endswith('_rate') or f['key'] in ('area_sq_ft','unit_value') else currency(f['value']))] for f in fields],[340,182]))
        mix=[]
        for field,value in v.items():
            if field.startswith('mix_') and field.endswith('_units') and isinstance(value,(int,float)) and value>0:
                prefix=field.removesuffix('_units')
                mix.append([f'Configuration {int(prefix.split("_")[1])}',display(value),display(v.get(prefix+'_monthly_rent'),'rent'),display(v.get(prefix+'_unit_area_sq_ft'))])
        if mix:
            story.extend([Spacer(1,16),p('Apartment unit mix','Heading2'),table([['Unit mix','Units','Monthly rent / unit','Unit area (sq ft)']]+mix,[150,82,145,145])])
        if detail['errors']:
            story.extend([Spacer(1,12),p('Calculation exceptions','Heading2'),p('\n'.join(f'{k.replace("_"," ")}: {error}' for k,error in detail['errors'].items()))])
        story.extend([PageBreak(),p('2026 COMMERCIAL APPRAISAL / ARB SUPPORT','SmallSource'),Spacer(1,8),p('Equity comparison','Title'),p(detail['account']+' / '+str(v.get('name') or detail['label']),'Heading2'),p(v.get('address') or 'Address not supplied in model'),p(v.get('category') or 'Unclassified','SmallSource'),Spacer(1,16)])
        equity=generate_equity(store,record_id);unit=equity['unit'];subject=equity['subject']
        story.append(table([['SUBJECT VALUE / '+unit.upper(),'COMPARABLE MEDIAN / '+unit.upper()],[display(subject['unit_value'],'unit_value'),display(equity['median_unit_value'],'unit_value')]],[261,261]))
        story.extend([Spacer(1,14),p(f"Automatically generated: {len(equity['comparables'])} displayed / {equity['eligible_count']} eligible accounts",'SmallSource')])
        if equity['missing_reason']:story.append(p(equity['missing_reason'],'SmallSource'))
        rows=[['Account / property',unit.title(),'Year built','Model value','Value / '+unit]]
        for comp in [subject]+equity['comparables']:
            cv=comp['values']
            rows.append([('SUBJECT / ' if comp is subject else '')+comp['account']+'\n'+str(cv.get('name') or comp['label'])+'\n'+str(cv.get('address') or 'Address not supplied')+('\n'+comp['match_label'] if comp is not subject else ''),display(comp['size']),str(cv.get('year_built') or 'Not supplied'),currency(comp['comparison_value']),display(comp['unit_value'],'unit_value')])
        story.extend([Spacer(1,12),table(rows,[190,70,65,105,92],compact=True),Spacer(1,14),p('How the comparisons were selected','Heading2'),p(equity['rules'],'SmallSource')])
        story.append(p('Comparable source rows: '+'; '.join(comp['account']+' / '+comp['source_sheet']+' '+str(comp['source_row']) for comp in equity['comparables'])+'.','SmallSource'))
        story.extend([Spacer(1,12),p('Interpreting the comparison','Heading2'),p('Median × subject size = '+currency(equity['unadjusted_indication'])+'. '+equity['explanation'],'SmallSource')])
        # Separate readable detail sheets preserve every supplied assumption and result.
        peers=[subject]+equity['comparables']
        for group_indexes in ((0,1),(2,3,4)):
            sections=[section for section in equity['sections'] if section['title'] in [
                'Property characteristics','Operating assumptions','Rent and space components',
                'Annual income and expenses','Value reconciliation'][group_indexes[0]:group_indexes[-1]+1]]
            if not sections:continue
            story.extend([PageBreak(),p('EQUITY / DETAILED MODEL DATA','SmallSource'),p(detail['account']+' / '+('Property & operating assumptions' if group_indexes[0]==0 else 'Income & value reconciliation'),'Heading2'),p('Current saved source values. Rent retains the source model basis. Not supplied is missing data; unavailable is a calculation exception.','SmallSource'),Spacer(1,10)])
            for section in sections:
                story.append(p(section['title'],'Heading2'))
                rows=[['Model field']+[('Subject' if n==0 else 'Comp '+str(n))+'\n'+r['account'] for n,r in enumerate(peers)]]
                for row in section['rows']:
                    cells=[]
                    for cell in row['cells']:
                        value=cell['value']
                        if cell['error']:text='Unavailable'
                        elif value is None:text='Not supplied'
                        elif row['format']=='rate':text=display(value,'cap_rate')
                        elif row['format']=='money':text=(f'(${abs(value):,.2f})' if value<0 else f'${value:,.2f}') if isinstance(value,(int,float)) else str(value)
                        elif row['format']=='text':text=str(value)
                        else:text=display(value)
                        cells.append(text)
                    rows.append([row['label']]+cells)
                story.extend([table(rows,[150]+[372/len(peers)]*len(peers),compact=True,dense=len(peers)>5),Spacer(1,8)])
            exceptions=[r['account']+' / '+row['label']+': '+cell['error'] for section in sections for row in section['rows'] for r,cell in zip(peers,row['cells']) if cell['error']]
            if exceptions:story.append(p('Calculation exceptions: '+'; '.join(exceptions),'SmallSource'))
            story.append(p('Source rows: '+'; '.join(r['account']+' / '+r['source_sheet']+' '+str(r['source_row']) for r in peers),'SmallSource'))
        if notes.strip():story.extend([Spacer(1,16),p('Appraiser notes','Heading2'),p(notes)])

    class PacketDocument(SimpleDocTemplate):
        def afterPage(self):
            canvas=self.canv;canvas.saveState();canvas.setFont('Helvetica',8);canvas.setFillColor(colors.HexColor('#64748b'));canvas.drawString(45,28,'Rendition Pilot / '+getattr(canvas,'_packet_account','Commercial appraisal'));canvas.drawRightString(567,28,f'Page {self.page}');canvas.restoreState()
    PacketDocument(stream,pagesize=letter,rightMargin=45,leftMargin=45,topMargin=42,bottomMargin=48,title='Commercial appraisal packets').build(story)
    return stream.getvalue()
