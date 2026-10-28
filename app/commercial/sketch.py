"""Editable account-level building sketches, independent of valuation model inputs."""
from __future__ import annotations
import base64
import io
import json
import math
import os
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from PIL import Image, ImageOps, UnidentifiedImageError

KINDS = {'main','addition','canopy','loading','walkway','detached','other'}
ROLES = {'overhead','front','rear','side','oblique','detail'}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS building_sketches(account TEXT PRIMARY KEY,revision INTEGER NOT NULL,document TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS building_sketch_photos(id TEXT PRIMARY KEY,account TEXT NOT NULL,metadata TEXT NOT NULL,image TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS sketch_photos_account_idx ON building_sketch_photos(account);
CREATE TABLE IF NOT EXISTS building_sketch_history(account TEXT NOT NULL,revision INTEGER NOT NULL,document TEXT NOT NULL,PRIMARY KEY(account,revision));
'''

class SketchError(ValueError): pass
class SketchConflict(SketchError): pass


def now():return datetime.now(timezone.utc).isoformat()


def blank(account):
    return dict(account=account,revision=0,status='DRAFT',components=[],calibration=None,
                overhead_id=None,photos=[],notes='',warnings=[],updated_at=None,updated_by=None,
                approved_at=None,approved_by=None)


def get(store, account):
    with store.connect() as db:
        row=db.execute('SELECT document FROM building_sketches WHERE account=?',(account,)).fetchone()
    return json.loads(row[0]) if row else blank(account)


def photos(store,account):
    with store.connect() as db:
        rows=db.execute('SELECT metadata,image FROM building_sketch_photos WHERE account=?',(account,)).fetchall()
    result=[]
    for row in rows:
        image=row[1]
        if image.startswith('remote:'):
            from .remote import object_request
            response=object_request('GET',store.remote_district,image.removeprefix('remote:'))
            try:response.raise_for_status()
            except Exception as exc:raise SketchError('A saved inspection photo could not load. Retry before editing this sketch.') from exc
            image='data:image/jpeg;base64,'+base64.b64encode(response.content).decode()
        result.append({**json.loads(row[0]),'data_url':image})
    return result


def point(value):
    if not isinstance(value,list) or len(value)!=2:raise SketchError('Each corner needs an x and y coordinate.')
    if any(isinstance(x,bool) or not isinstance(x,(int,float)) or not math.isfinite(x) or not 0<=x<=1000 for x in value):
        raise SketchError('Corners must fall inside the sketch canvas.')
    return [round(float(x),4) for x in value]


def area(points):return abs(sum(a[0]*b[1]-b[0]*a[1] for a,b in zip(points,points[1:]+points[:1])))/2


def cross(a,b,c):return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])


def intersects(a,b,c,d):
    def on(a,b,c):return abs(cross(a,b,c))<1e-7 and min(a[0],b[0])-1e-7<=c[0]<=max(a[0],b[0])+1e-7 and min(a[1],b[1])-1e-7<=c[1]<=max(a[1],b[1])+1e-7
    return (cross(a,b,c)*cross(a,b,d)<0 and cross(c,d,a)*cross(c,d,b)<0) or on(a,b,c) or on(a,b,d) or on(c,d,a) or on(c,d,b)


def polygon(values):
    if not isinstance(values,list) or not 3<=len(values)<=64:raise SketchError('Each component needs 3–64 corners.')
    points=[point(p) for p in values]
    if len({tuple(p) for p in points})!=len(points) or area(points)<1:raise SketchError('A component must have distinct corners and a usable area.')
    for i in range(len(points)):
        for j in range(i+1,len(points)):
            if j==i+1 or (i==0 and j==len(points)-1):continue
            if intersects(points[i],points[(i+1)%len(points)],points[j],points[(j+1)%len(points)]):raise SketchError('Component edges cannot cross or touch themselves.')
    return points


def component_overlap(components):
    from shapely.geometry import Polygon
    polygons=[Polygon(c['points']) for c in components]
    for i,a in enumerate(polygons):
        for b in polygons[i+1:]:
            if a.intersection(b).area>max(1e-4,min(a.area,b.area)*.005):return True
    return False


def measurement_check_result(document,available):
    calibration=document.get('calibration');check=document.get('measurement_check')
    if not calibration or not check:return {'passed':False,'message':'Confirm a second measured wall to cross-check the footprint proportions.'}
    overhead=next((p for p in available if p['id']==document.get('overhead_id')),None)
    w,h=(overhead['width'],overhead['height']) if overhead else (1000,700)
    def vector(ref):return [(ref['b'][0]-ref['a'][0])*w/1000,(ref['b'][1]-ref['a'][1])*h/1000]
    u,v=vector(calibration),vector(check);lu,lv=math.hypot(*u),math.hypot(*v)
    if not lu or not lv or abs(u[0]*v[1]-u[1]*v[0])/(lu*lv)<math.sin(math.radians(15)):
        return {'passed':False,'message':'Use a second wall in a different direction from the scale reference.'}
    is_wall=any((math.dist(check['a'],a)<.01 and math.dist(check['b'],b)<.01) or (math.dist(check['b'],a)<.01 and math.dist(check['a'],b)<.01) for c in document['components'] for a,b in zip(c['points'],c['points'][1:]+c['points'][:1]))
    if not is_wall:return {'passed':False,'message':'The checked wall changed. Re-select and verify the second wall.'}
    predicted=lv/lu*calibration['feet'];error=abs(predicted-check['feet']);tolerance=max(.5,check['feet']*.02)
    passed=bool(check['verified'] and calibration['verified'] and error<=tolerance)
    return {'passed':passed,'predicted_feet':round(predicted,2),'measured_feet':check['feet'],'difference_feet':round(error,2),'tolerance_feet':round(tolerance,2),'message':f"Sketch predicts {predicted:.1f} ft; measured {check['feet']:.1f} ft. "+('Cross-check passed.' if passed else 'Proportions need correction before approval.')}


def validate(document, available, *, approve=False):
    if not isinstance(document,dict):raise SketchError('Invalid sketch document.')
    components=document.get('components',[])
    if not isinstance(components,list) or len(components)>40:raise SketchError('Use at most 40 components per sketch.')
    clean=[];seen=set()
    for comp in components:
        if not isinstance(comp,dict) or comp.get('kind') not in KINDS:raise SketchError('Choose a valid building component type.')
        identity=str(comp.get('id') or uuid.uuid4().hex)
        if len(identity)>80 or identity in seen:raise SketchError('Component identities must be unique.')
        code=str(comp.get('area_code') or '').strip()
        if code and (not code.isascii() or not code.isdigit() or len(code)>12):raise SketchError('Area codes must contain up to 12 digits.')
        seen.add(identity)
        clean.append(dict(id=identity,label=str(comp.get('label') or 'Building component')[:100],kind=comp['kind'],
                          area_code=code,area_description=str(comp.get('area_description') or '')[:200],points=polygon(comp.get('points')),source=comp.get('source') if comp.get('source') in ('ai','public_gis') else 'manual',verified=comp.get('verified') is True))
    overhead=document.get('overhead_id')
    ids={p['id'] for p in available}
    if overhead and (overhead not in ids or not any(p['id']==overhead and p['role']=='overhead' for p in available)):raise SketchError('The overhead photo is not attached to this account.')
    calibration=document.get('calibration')
    if calibration:
        if not isinstance(calibration,dict):raise SketchError('Invalid measurement scale.')
        a,b=point(calibration.get('a')),point(calibration.get('b'))
        distance=calibration.get('feet')
        if a==b or isinstance(distance,bool) or not isinstance(distance,(int,float)) or not math.isfinite(distance) or not 0<distance<=100000:
            raise SketchError('Enter a positive measured distance between two different scale points.')
        calibration=dict(a=a,b=b,feet=float(distance),verified=calibration.get('verified') is True)
    check=document.get('measurement_check')
    if check:
        if not isinstance(check,dict):raise SketchError('Invalid independent wall measurement.')
        a,b=point(check.get('a')),point(check.get('b'));feet=check.get('feet')
        if a==b or isinstance(feet,bool) or not isinstance(feet,(int,float)) or not math.isfinite(feet) or not 0<feet<=100000:
            raise SketchError('Enter a positive independent wall measurement.')
        check=dict(a=a,b=b,feet=float(feet),verified=check.get('verified') is True)
    else:check=None
    if approve and any(c['source']=='public_gis' and c['kind']=='other' for c in clean):raise SketchError('Classify every public GIS building component before approval.')
    if approve and component_overlap(clean):raise SketchError('Building components overlap and would double-count area. Correct the outlines before approval.')
    if approve and any(c['source']=='ai' for c in clean):
        checked=measurement_check_result({**document,'components':clean,'calibration':calibration,'measurement_check':check},available)
        if not checked['passed']:raise SketchError(checked['message'])
    if approve and (not clean or not calibration or not calibration['verified'] or not all(c['verified'] for c in clean)):
        raise SketchError('Approve only after setting a verified measured scale and verifying every component.')
    warnings=document.get('warnings',[])
    if not isinstance(warnings,list):warnings=[]
    source=next((p.get('provenance') for p in available if p['id']==overhead),None)
    return dict(area_context=document.get('area_context'),components=clean,overhead_id=overhead,calibration=calibration,measurement_check=check,public_source=source,generation_quality=document.get('generation_quality') if isinstance(document.get('generation_quality'),dict) else None,notes=str(document.get('notes') or '')[:4000],warnings=[str(w)[:500] for w in warnings[:20]])


def summary(document, available):
    overhead=next((p for p in available if p['id']==document.get('overhead_id')),None)
    width,height=(overhead['width'],overhead['height']) if overhead else (1000,700)
    calibration=document.get('calibration');factor=None
    if calibration:
        a,b=calibration['a'],calibration['b']
        length=math.hypot((b[0]-a[0])*width/1000,(b[1]-a[1])*height/1000)
        if length>0:factor=calibration['feet']/length
    result=[]
    for c in document['components']:
        points=[[x*width/1000,y*height/1000] for x,y in c['points']]
        result.append(dict(id=c['id'],square_feet=round(area(points)*factor**2,2) if factor else None,
                           wall_feet=[round(math.dist(a,b)*factor,2) for a,b in zip(points,points[1:]+points[:1])] if factor else []))
    source=document.get('public_source')
    public_note=(' Public GIS-derived dimensions, not field measured. '+str(source['provider'])+'; imagery service '+str(source['imagery_year'])+'; footprint dates '+', '.join(sorted({f['footprint_date'] for f in source['footprints']}))+'.') if source else ''
    return dict(components=result,scaled=factor is not None,width=width,height=height,
                measurement_check=measurement_check_result(document,available),area_note='Component areas are separate; overlapping components cannot be approved. Roof edges and wall footprints may differ.'+public_note)


def _write(db,account,document,revision,actor,approve):
    stamp=now();saved={**document,'account':account,'revision':revision+1,'status':'APPROVED' if approve else 'DRAFT',
                        'updated_at':stamp,'updated_by':actor,'approved_at':stamp if approve else None,'approved_by':actor if approve else None}
    encoded=json.dumps(saved,allow_nan=False)
    db.execute('INSERT INTO building_sketches VALUES(?,?,?) ON CONFLICT(account) DO UPDATE SET revision=excluded.revision,document=excluded.document',(account,revision+1,encoded))
    db.execute('INSERT INTO building_sketch_history VALUES(?,?,?)',(account,revision+1,encoded))
    return saved


def save(store,account,document,revision,actor,approve=False):
    with store.lock,store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        current=db.execute('SELECT revision FROM building_sketches WHERE account=?',(account,)).fetchone()
        if (current[0] if current else 0)!=revision:raise SketchConflict('This sketch changed in another session. Reload it before saving; your device draft is retained.')
        available=[json.loads(r[0]) for r in db.execute('SELECT metadata FROM building_sketch_photos WHERE account=?',(account,))]
        clean=validate(document,available,approve=approve)
        return _write(db,account,clean,revision,actor,approve)


def upload(store,account,encoded,role,caption,revision,actor):
    if role not in ROLES:raise SketchError('Choose a photo role.')
    try:
        if not isinstance(encoded,str) or len(encoded)>4_000_000:raise SketchError('Choose an image smaller than 3 MB.')
        raw=base64.b64decode(encoded.split(',',1)[-1],validate=True)
        with Image.open(io.BytesIO(raw)) as source:
            if source.format not in ('JPEG','PNG','WEBP'):raise SketchError('Choose a JPEG, PNG, or WebP photo.')
            if source.width*source.height>30_000_000:raise SketchError('The image is too large. Resize it before uploading.')
            image=ImageOps.exif_transpose(source).convert('RGB');image.thumbnail((2400,2400));out=io.BytesIO();image.save(out,format='JPEG',quality=90 if role=='overhead' else 85)
    except (ValueError,OSError,UnidentifiedImageError,Image.DecompressionBombError) as exc:raise SketchError('Choose a valid JPEG, PNG, or WebP photo.') from exc
    photo=dict(id=uuid.uuid4().hex,role=role,caption=str(caption or role)[:150],width=image.width,height=image.height,created_at=now())
    image_url='data:image/jpeg;base64,'+base64.b64encode(out.getvalue()).decode()
    photo['bytes']=len(image_url)
    with store.lock,store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT revision,document FROM building_sketches WHERE account=?',(account,)).fetchone()
        if (row[0] if row else 0)!=revision:raise SketchConflict('The sketch changed. Reload before adding a photo.')
        existing=db.execute('SELECT metadata,image FROM building_sketch_photos WHERE account=?',(account,)).fetchall()
        photo_bytes=sum(json.loads(row[0]).get('bytes',len(row[1])) for row in existing)
        if len(existing)>=12 or photo_bytes+len(image_url)>10_000_000:raise SketchError('This account has reached its photo limit (12 photos / 10 MB).')
        doc=json.loads(row[1]) if row else blank(account)
        if role=='overhead' and doc['components']:raise SketchError('An overhead image must be attached before drawing components; start a new draft if its image needs replacing.')
        db.execute('INSERT INTO building_sketch_photos VALUES(?,?,?,?)',(photo['id'],account,json.dumps(photo),image_url))
        if role=='overhead':doc['overhead_id']=photo['id'];doc['calibration']=None
        return _write(db,account,validate(doc,[photo]+[json.loads(r[0]) for r in db.execute('SELECT metadata FROM building_sketch_photos WHERE account=?',(account,))]),revision,actor,False)


def history(store,account):
    with store.connect() as db:
        rows=db.execute('SELECT revision,document FROM building_sketch_history WHERE account=? ORDER BY revision DESC LIMIT 25',(account,)).fetchall()
    return [dict(revision=r[0],status=(d:=json.loads(r[1]))['status'],updated_at=d['updated_at'],updated_by=d['updated_by']) for r in rows]


def generate(document, available):
    from .sketch_ai import generate as focused_generate
    return focused_generate(document,available)


def area_context(record):
    return {'model':str(record.get('model') or ''),'category':str(record.get('values',{}).get('category') or '').strip().casefold()}


def area_defaults(store, record, seeded=None):
    """District-local defaults from explicit codes saved for the same model/category."""
    context=area_context(record);choices={}
    if not context['model'] or not context['category']:return seeded or {}
    with store.connect() as db:
        rows=db.execute('SELECT document FROM building_sketches').fetchall()
    for row in rows:
        doc=json.loads(row[0])
        if doc.get('area_context')!=context:continue
        for comp in doc.get('components',[]):
            if comp.get('area_code') and comp.get('area_description') and comp.get('verified'):
                choices.setdefault(comp['kind'],set()).add((comp['area_code'],comp['area_description']))
    # Ambiguous category mappings require a choice rather than guessing.
    return {**{kind:value for kind,value in (seeded or {}).items() if kind not in choices},**{kind:{'area_code':next(iter(values))[0],'area_description':next(iter(values))[1]} for kind,values in choices.items() if len(values)==1}}


def export_apex_xml(document, available):
    """Area summary interchange matching the supplied Apex v6 plaza.XML; no vectors."""
    from xml.etree.ElementTree import Element, SubElement, tostring, indent
    clean=validate(document,available,approve=True)
    sizes=summary(clean,available)
    if not clean['components'] or not sizes['scaled']:raise SketchError('Measure and verify the building sections before exporting XML.')
    for comp in clean['components']:
        if not comp['area_code']:raise SketchError('Enter an Orion area code for every building section before exporting XML.')
    account=str(document.get('account') or 'sketch')
    root=Element('tns:sketch',{'name':account+'.AX6','unit':'foot','xmlns:tns':'urn:xmlns:apex:sketch:xml'})
    SubElement(root,'subject')
    for index in range(1,4):
        table=SubElement(root,'commentTable',{'index':str(index)})
        for _ in range(10):SubElement(table,'comment',{'value':''})
    page=SubElement(root,'page',{'index':'0'})
    for index,(comp,size) in enumerate(zip(clean['components'],sizes['components'])):
        node=SubElement(page,'area',{'code':comp['area_code'],'index':str(index),'name':comp['label'],'uniqueId':str(index)})
        values=[('actualSize',f"{size['square_feet']:.2f}"),('description',comp['area_description']),('multiplier','1'),('netSize',f"{size['square_feet']:.2f}"),('perimeter',f"{sum(size['wall_feet']):.2f}"),('sign','1')]
        for key,value in values:SubElement(node,key).text=value
    indent(root,space='  ')
    return b'\xef\xbb\xbf'+tostring(root,encoding='utf-8',xml_declaration=True).replace(b'\n',b'\r\n')


def export_svg(document, available):
    from xml.etree.ElementTree import Element, SubElement, tostring
    sizes=summary(document,available);ratio=sizes['height']/sizes['width'];height=1000*ratio
    root=Element('svg',xmlns='http://www.w3.org/2000/svg',viewBox=f'0 0 1000 {height+125}')
    if document.get('public_source'):SubElement(root,'metadata').text=json.dumps(document['public_source'])
    SubElement(root,'rect',width='1000',height=str(height+125),fill='#fff')
    for i,comp in enumerate(document['components']):
        points=comp['points'];color=['#256650','#b17b29','#447b95','#a05268'][i%4]
        SubElement(root,'polygon',points=' '.join(f'{x},{y*ratio}' for x,y in points),fill=color,stroke=color,**{'fill-opacity':'0.12','stroke-width':'2'})
        x=sum(p[0] for p in points)/len(points);y=sum(p[1] for p in points)/len(points)*ratio
        area_sq=sizes['components'][i]['square_feet']
        label=SubElement(root,'text',x=str(x),y=str(y),fill='#172d31',**{'text-anchor':'middle','font-size':'16'})
        label.text=f"{comp['label']} · {area_sq:,.1f} SF" if area_sq is not None else comp['label']+' · UNSCALED'
    title=SubElement(root,'text',x='15',y=str(height+32),**{'font-size':'20'});title.text=f"{document['account']} — {document['status']} — Revision {document['revision']}"
    note=SubElement(root,'text',x='15',y=str(height+59),**{'font-size':'13'});note.text='Appraisal sketch · '+('GIS-derived dimensions; not field measured' if document.get('public_source') else 'Approved measured sketch' if document['status']=='APPROVED' else 'Draft reference scale; geometry unverified') if sizes['scaled'] else 'No measurement scale; dimensions unavailable'
    from textwrap import wrap
    note=SubElement(root,'text',x='15',y=str(height+83),**{'font-size':'12'})
    for index,line in enumerate(wrap(sizes['area_note'],width=145)[:3]):SubElement(note,'tspan',x='15',dy='0' if index==0 else '14').text=line
    return tostring(root,encoding='utf-8',xml_declaration=True)


def export_pdf(document,available):
    from reportlab.pdfgen.canvas import Canvas
    from reportlab.lib.pagesizes import landscape,letter
    from reportlab.lib.colors import HexColor
    from reportlab.lib.utils import simpleSplit
    output=io.BytesIO();page_width,page_height=landscape(letter)
    canvas=Canvas(output,pagesize=(page_width,page_height));canvas.setTitle(document['account']+' building sketch')
    metrics=summary(document,available);ratio=metrics['height']/metrics['width'];drawing_height=1000*ratio
    def header():
        canvas.setFillColor(HexColor('#172d31'));canvas.setFont('Helvetica-Bold',16)
        canvas.drawString(36,page_height-35,document['account']+' - Building sketch')
        canvas.setFont('Helvetica',9);canvas.drawString(36,page_height-51,f"{document['status']} | Revision {document['revision']} | Saved by {document.get('updated_by') or '-'}")
    header();factor=min((page_width-90)/1000,330/drawing_height);offset_x=(page_width-1000*factor)/2;offset_y=page_height-70-drawing_height*factor
    for i,comp in enumerate(document['components']):
        color=HexColor(['#256650','#ad7824','#357a99','#aa5875'][i%4]);canvas.setStrokeColor(color);canvas.setLineWidth(1.5)
        path=canvas.beginPath()
        for j,(x,y) in enumerate(comp['points']):
            (path.moveTo if j==0 else path.lineTo)(offset_x+x*factor,offset_y+(drawing_height-y*ratio)*factor)
        path.close();canvas.drawPath(path,fill=0,stroke=1)
        x=sum(p[0] for p in comp['points'])/len(comp['points']);y=sum(p[1] for p in comp['points'])/len(comp['points'])
        canvas.setFillColor(color);canvas.setFont('Helvetica',8);canvas.drawCentredString(offset_x+x*factor,offset_y+(drawing_height-y*ratio)*factor,comp['label'][:60])
    y=offset_y-24;canvas.setFillColor(HexColor('#172d31'));canvas.setFont('Helvetica',9)
    note=('Verified scale' if document.get('calibration',{}).get('verified') else 'Unverified scale') if document.get('calibration') else 'UNSCALED - dimensions and areas unavailable'
    canvas.drawString(36,y,note+' | '+('Approved by '+document['approved_by'] if document.get('approved_by') else 'Draft - not approved'));y-=18
    if document.get('measurement_check'):
        canvas.setFont('Helvetica',8);canvas.drawString(36,y,metrics['measurement_check']['message'][:140]);y-=18
    for comp,values in zip(document['components'],metrics['components']):
        if y<50:canvas.showPage();header();y=page_height-80
        value=f"{values['square_feet']:,.1f} SF" if values['square_feet'] is not None else 'Unscaled'
        canvas.setFont('Helvetica-Bold',9);canvas.drawString(36,y,f"{comp['label'][:70]} | {comp['kind']} | {value} | {'Verified' if comp['verified'] else 'Needs verification'}");y-=13
        canvas.setFont('Helvetica',8)
        text='Walls: '+', '.join(f'{v:.1f} ft' for v in values['wall_feet']) if values['wall_feet'] else 'No measured wall lengths.'
        for line in simpleSplit(text,'Helvetica',8,page_width-72):
            if y<40:canvas.showPage();header();y=page_height-80
            canvas.drawString(36,y,line);y-=11
        y-=8
    for line in simpleSplit(metrics['area_note']+' '+str(document.get('notes') or ''),'Helvetica',8,page_width-72):
        if y<40:canvas.showPage();header();y=page_height-80
        canvas.setFont('Helvetica',8);canvas.drawString(36,y,line);y-=11
    canvas.save();return output.getvalue()


def import_public(store,account,bundle,revision,actor,replace=False):
    """Persist trusted server-fetched geometry and its image in one revision."""
    photo=deepcopy(bundle['photo']);encoded=photo.pop('data_url');photo['bytes']=len(encoded)
    with store.lock,store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT revision,document FROM building_sketches WHERE account=?',(account,)).fetchone()
        if (row[0] if row else 0)!=revision:raise SketchConflict('The sketch changed. Reload before importing public measurements.')
        current=json.loads(row[1]) if row else blank(account)
        if current['components'] and not replace:raise SketchConflict('Confirm replacement of the existing sketch before importing a new draft.')
        existing=[json.loads(r[0]) for r in db.execute('SELECT metadata FROM building_sketch_photos WHERE account=?',(account,))]
        if len(existing)>=12 or sum(p.get('bytes',0) for p in existing)+len(encoded)>10_000_000:raise SketchError('This account has reached its photo storage limit.')
        clean=validate(bundle['document'],existing+[photo])
        db.execute('INSERT INTO building_sketch_photos VALUES(?,?,?,?)',(photo['id'],account,json.dumps(photo),encoded))
        return _write(db,account,clean,revision,actor,False)
