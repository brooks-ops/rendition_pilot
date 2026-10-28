"""Free, parcel-linked Lubbock GIS measurement drafts in State Plane US survey feet."""
import base64
import io
import math
import re
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse
import requests
from PIL import Image
from shapely.geometry import Polygon
from .sketch import SketchError

ROOT='https://pubgis.ci.lubbock.tx.us/server/rest/services'
PARCEL=ROOT+'/ParcelViewerSearch/MapServer/2/query'
BUILDINGS=ROOT+'/ParcelViewerSearch/MapServer/1/query'
IMAGERY=ROOT+'/Imagery/Lubbock_Image_Cache_2025_StatePlane/ImageServer'


def fetch(url, params=None, binary=False):
    try:
        response=requests.get(url,params=params,timeout=(5,25),allow_redirects=False)
        response.raise_for_status()
        if response.is_redirect:raise ValueError('Unexpected redirect')
        if binary:
            if len(response.content)>4_000_000:raise ValueError('Image too large')
            return response.content
        data=response.json()
        if data.get('error'):raise ValueError('GIS request failed')
        if data.get('exceededTransferLimit'):raise ValueError('GIS result was truncated')
        return data
    except (requests.RequestException,ValueError) as exc:
        raise SketchError('The public GIS service could not complete this request. Your saved sketch is unchanged; try again later.') from exc


def date(value):
    if not isinstance(value,(int,float)) or not math.isfinite(value) or value<=0:return 'Not supplied'
    try:return datetime.fromtimestamp(value/1000,timezone.utc).date().isoformat()
    except (ValueError,OverflowError,OSError):return 'Not supplied'


def build(account):
    if not isinstance(account,str) or not re.fullmatch(r'R\d{1,12}',account):raise SketchError('A valid R account number is required.')
    parcels=fetch(PARCEL,{'f':'json','where':f"LCADID='{account}'",'outFields':'PIN,LCADID','returnGeometry':'true','outSR':2276}).get('features',[])
    if len(parcels)!=1:raise SketchError('No unique Lubbock GIS parcel matches this account. No neighboring building was substituted.')
    parcel=parcels[0]
    if parcel.get('attributes',{}).get('LCADID')!=account:raise SketchError('The GIS parcel account does not match this property.')
    pin=str(parcel['attributes'].get('PIN',''))
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}',pin):raise SketchError('This parcel has no usable building link.')
    features=fetch(BUILDINGS,{'f':'json','where':f"PIN='{pin}'",'outFields':'OBJECTID,PIN,STRUCTURE_NAME,DATE_COLLECTED,SOURCE','returnGeometry':'true','outSR':2276}).get('features',[])
    if not features:raise SketchError('The city has no building footprint for this parcel. Automatic measurement is unavailable here; no outline was invented.')
    if len(features)>30:raise SketchError('This parcel has too many building components for one sketch. Review it separately.')
    rings=parcel.get('geometry',{}).get('rings',[])
    if len(rings)!=1:raise SketchError('This parcel has complex geometry and needs a separate location review.')
    boundary=Polygon(rings[0]);shapes=[]
    for feature in features:
        if str(feature.get('attributes',{}).get('PIN'))!=pin:raise SketchError('A GIS building link does not match this parcel.')
        rings=feature.get('geometry',{}).get('rings',[])
        if len(rings)!=1:raise SketchError('A footprint contains a courtyard or multiple rings. Manual geometry review is required; its area was not filled in.')
        shape=Polygon(rings[0])
        if not shape.is_valid or shape.area<=0 or not all(math.isfinite(v) for v in shape.bounds):raise SketchError('The city returned an invalid footprint.')
        if len(shape.exterior.coords)>65:raise SketchError('A footprint is too detailed for this sketch editor. No simplified area was substituted.')
        if not boundary.is_valid or boundary.intersection(shape).area/shape.area<.95:raise SketchError('A linked building does not fit this parcel. Confirm the account location before measuring.')
        shapes.append((shape,feature['attributes']))
    shapes.sort(key=lambda pair:pair[0].area,reverse=True)
    xmin=min(p.bounds[0] for p,_ in shapes);ymin=min(p.bounds[1] for p,_ in shapes)
    xmax=max(p.bounds[2] for p,_ in shapes);ymax=max(p.bounds[3] for p,_ in shapes)
    side=max(xmax-xmin,ymax-ymin)+60
    if side>3000:raise SketchError('The buildings span too large an area for a useful single overhead. Review them separately.')
    x=(xmin+xmax)/2;y=(ymin+ymax)/2;bounds=[x-side/2,y-side/2,x+side/2,y+side/2]
    meta=fetch(IMAGERY+'/exportImage',{'f':'json','bbox':','.join(str(v) for v in bounds),'bboxSR':2276,'imageSR':2276,'size':'1600,1600','format':'jpg','adjustAspectRatio':'false','noData':0,'noDataInterpretation':'esriNoDataMatchAll'})
    href=meta.get('href','');parsed=urlparse(href)
    if parsed.scheme!='https' or parsed.hostname!='pubgis.ci.lubbock.tx.us' or parsed.port not in (None,443):raise SketchError('The imagery service returned an unexpected image location.')
    extent=meta.get('extent',{});sr=extent.get('spatialReference',{})
    if (sr.get('latestWkid') or sr.get('wkid')) not in (2276,102738):raise SketchError('The image coordinate system is not State Plane feet.')
    try:
        xmin,ymin,xmax,ymax=[float(extent[k]) for k in ('xmin','ymin','xmax','ymax')]
        if not all(math.isfinite(v) for v in (xmin,ymin,xmax,ymax)) or xmax<=xmin or ymax<=ymin:raise ValueError()
        raw=fetch(href,binary=True)
        with Image.open(io.BytesIO(raw)) as image:
            if image.width*image.height>6_000_000 or min(image.size)<500:raise ValueError()
            width,height=image.size
            if abs((xmax-xmin)/width-(ymax-ymin)/height)>1e-5:raise ValueError()
            out=io.BytesIO();image.convert('RGB').save(out,'JPEG',quality=90)
    except (ValueError,OSError,KeyError) as exc:raise SketchError('The overhead image could not be calibrated reliably. No guessed scale was used.') from exc
    components=[];sources=[]
    for shape,attributes in shapes:
        points=[[(a-xmin)/(xmax-xmin)*1000,(ymax-b)/(ymax-ymin)*1000] for a,b in list(shape.exterior.coords)[:-1]]
        if any(not 0<=v<=1000 for point in points for v in point):raise SketchError('The imagery extent does not contain the building.')
        components.append(dict(id=uuid.uuid4().hex,label=str(attributes.get('STRUCTURE_NAME') or f'Building {len(components)+1}')[:100],kind='other',points=points,source='public_gis',verified=False))
        sources.append(dict(object_id=attributes.get('OBJECTID'),footprint_date=date(attributes.get('DATE_COLLECTED')),provider=attributes.get('SOURCE') or 'Not supplied',square_feet=round(shape.area,2)))
    provenance=dict(provider='City of Lubbock public GIS',account=account,parcel_pin=pin,footprint_service=BUILDINGS,imagery_service=IMAGERY,imagery_year=2025,footprints=sources,spatial_reference=2276,units='US survey feet',extent=[xmin,ymin,xmax,ymax],retrieved_at=datetime.now(timezone.utc).isoformat(),accuracy='Source geometry; not field verified')
    photo=dict(id=uuid.uuid4().hex,role='overhead',caption='Lubbock public imagery · 2025 service',width=width,height=height,created_at=provenance['retrieved_at'],provenance=provenance)
    photo['data_url']='data:image/jpeg;base64,'+base64.b64encode(out.getvalue()).decode()
    warnings=['Public GIS draft: compare every outline with the imagery. Roof edges, walls, additions and component types need review.','Footprint dates may be older than the imagery. Image service year is not a confirmed capture date.','Areas are building footprints, not total floor area. Unknown component types must be classified before approval.']
    draft=dict(components=components,overhead_id=photo['id'],calibration=dict(a=[0,500],b=[1000,500],feet=xmax-xmin,verified=False),measurement_check=None,notes='',warnings=warnings,public_source=provenance)
    return {'document':draft,'photo':photo,'provenance':provenance}
