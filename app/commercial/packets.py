from io import BytesIO
from datetime import datetime
from zoneinfo import ZoneInfo
from html import escape
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


def build_packet(store,ids,comparison_ids=None,notes=''):
    if not 1<=len(ids)<=100:raise ValueError('Select between 1 and 100 model rows per packet batch')
    stream=BytesIO();styles=getSampleStyleSheet()
    styles.add(ParagraphStyle(name='SmallSource',fontName='Helvetica',fontSize=8,leading=11,textColor=colors.HexColor('#566478')))
    styles['Title'].fontName='Helvetica-Bold';styles['Title'].fontSize=24;styles['Title'].alignment=0
    story=[]
    def p(text,style='BodyText'):return Paragraph(escape(str(text)).replace('\n','<br/>'),styles[style])
    def table(rows,widths):
        t=Table([[p(cell) for cell in row] for row in rows],colWidths=widths,repeatRows=1,hAlign='LEFT')
        t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#e9eef3')),('VALIGN',(0,0),(-1,-1),'TOP'),('BOTTOMPADDING',(0,0),(-1,-1),8),('TOPPADDING',(0,0),(-1,-1),8),('LINEBELOW',(0,0),(-1,0),0.7,colors.HexColor('#94a3b8')),('LINEBELOW',(0,1),(-1,-1),0.3,colors.HexColor('#e2e8f0'))]))
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
        story.extend([Spacer(1,18),p('Cost summary' if detail['model']=='cost' else 'Pro Forma','Heading2')])
        keys=['land_value','improvement_value','total_value','area_sq_ft','unit_value'] if detail['model']=='cost' else ['projected_gross_rent','vacancy_loss','effective_gross_rent','other_income_amount','bad_debt_amount','effective_gross_income','total_expenses','reserve_amount','noi','cap_rate','tax_rate','loaded_cap_rate','indicated_value','other_improvements','other_land','other_adjustments','land_adjustment','other_component_value','other_component_land_value','total_value']
        by_key={f['key']:f for f in detail['fields']}
        fields=[by_key[k] for k in keys if k in by_key and (by_key[k]['value'] is not None or by_key[k]['error'])]
        story.append(table([['Model field','Current value']]+[[f['label'],('Unavailable - '+f['error']) if f['error'] else display(f['value'],f['key'])] for f in fields],[340,182]))
        mix=[]
        for field,value in v.items():
            if field.startswith('mix_') and field.endswith('_units') and isinstance(value,(int,float)) and value>0:
                prefix=field.removesuffix('_units')
                mix.append([f'Configuration {int(prefix.split("_")[1])}',display(value),display(v.get(prefix+'_monthly_rent'),'rent'),display(v.get(prefix+'_unit_area_sq_ft'))])
        if mix:
            story.extend([Spacer(1,16),p('Apartment unit mix','Heading2'),table([['Unit mix','Units','Monthly rent / unit','Unit area (sq ft)']]+mix,[150,82,145,145])])
        if detail['errors']:
            story.extend([Spacer(1,12),p('Calculation exceptions','Heading2'),p('\n'.join(f'{k.replace("_"," ")}: {error}' for k,error in detail['errors'].items()))])
        selected=[c for c in (comparison_ids or []) if c!=record_id]
        if selected:
            story.extend([Spacer(1,16),p('Selected equity comparisons','Heading2')])
            rows=[['Account / model','Area (sq ft)','Model value','Value / sq ft']]
            for rid in selected[:12]:
                c=store.detail(rid);cv=c['values'];area=cv.get('area_sq_ft') or cv.get('leasable_area_sq_ft');value=cv.get('total_value') if cv.get('total_value') is not None else cv.get('indicated_value')
                rows.append([f"{cv.get('account')} / {c['label']}",display(area),display(value),display(value/area,'unit_value') if isinstance(area,(int,float)) and area>0 and isinstance(value,(int,float)) else 'Unavailable'])
            story.append(table(rows,[230,90,100,102]))
        if notes.strip():story.extend([Spacer(1,16),p('Appraiser notes','Heading2'),p(notes)])
        story.extend([Spacer(1,16),p('Values reflect the imported model and saved workspace changes. Supporting sales, photos and external evidence should be attached before final hearing use.','SmallSource')])
    class PacketDocument(SimpleDocTemplate):
        def afterPage(self):
            canvas=self.canv;canvas.saveState();canvas.setFont('Helvetica',8);canvas.setFillColor(colors.HexColor('#64748b'));canvas.drawString(45,28,'Rendition Pilot / '+getattr(canvas,'_packet_account','Commercial appraisal'));canvas.drawRightString(567,28,f'Page {self.page}');canvas.restoreState()
    PacketDocument(stream,pagesize=letter,rightMargin=45,leftMargin=45,topMargin=42,bottomMargin=48,title='Commercial appraisal packets').build(story)
    return stream.getvalue()
