import io
from copy import deepcopy
from PIL import Image
import pytest
from app.commercial import public_measure as pm,sketch
from tests.test_commercial_workspace import store


def service(monkeypatch,rings=None):
    image=io.BytesIO();Image.new('RGB',(1600,1600),'gray').save(image,'JPEG')
    def fake(url,params=None,binary=False):
        if url==pm.PARCEL:return {'features':[{'attributes':{'PIN':'42','LCADID':'R1'},'geometry':{'rings':[[[0,0],[200,0],[200,200],[0,200],[0,0]]]}}]}
        if url==pm.BUILDINGS:return {'features':[{'attributes':{'OBJECTID':1,'PIN':'42','DATE_COLLECTED':1515628800000},'geometry':{'rings':rings or [[[20,20],[120,20],[120,70],[20,70],[20,20]]]}}]}
        if url==pm.IMAGERY+'/exportImage':return {'href':'https://pubgis.ci.lubbock.tx.us/output/a.jpg','extent':{'xmin':0,'ymin':0,'xmax':200,'ymax':200,'spatialReference':{'wkid':2276}}}
        if binary:return image.getvalue()
        raise AssertionError(url)
    monkeypatch.setattr(pm,'fetch',fake)
    return fake


def test_measures_from_returned_extent_not_requested_extent(monkeypatch):
    service(monkeypatch);bundle=pm.build('R1');metrics=sketch.summary(bundle['document'],[bundle['photo']])
    assert metrics['components'][0]['square_feet']==5000
    assert metrics['components'][0]['wall_feet']==[100,50,100,50]
    assert bundle['document']['calibration']['feet']==200
    assert bundle['provenance']['footprints'][0]['footprint_date']=='2018-01-11'
    assert bundle['document']['calibration']['verified'] is False


def test_no_neighbor_substitution_and_invalid_account(monkeypatch):
    monkeypatch.setattr(pm,'fetch',lambda *a,**k:{'features':[]})
    with pytest.raises(sketch.SketchError,match='unique'):pm.build('R1')
    with pytest.raises(sketch.SketchError,match='valid R'):pm.build("R1' OR 1=1")


def test_holes_are_not_silently_filled(monkeypatch):
    service(monkeypatch,rings=[[[20,20],[120,20],[120,70],[20,70],[20,20]],[[30,30],[40,30],[40,40],[30,40],[30,30]]])
    with pytest.raises(sketch.SketchError,match='courtyard'):pm.build('R1')


def test_import_is_atomic_revisioned_and_provenance_survives_edits(store,monkeypatch):
    service(monkeypatch);bundle=pm.build('R1')
    saved=store.import_public_sketch('R1',bundle,0,'appraiser')
    assert saved['revision']==1 and saved['status']=='DRAFT'
    available=sketch.photos(store,'R1')
    assert available[0]['provenance']['parcel_pin']=='42'
    assert sketch.summary(saved,available)['components'][0]['square_feet']==5000
    assert sketch.get(store,'R2')['revision']==0
    with pytest.raises(sketch.SketchConflict):store.import_public_sketch('R1',bundle,0,'other')
    assert len(sketch.photos(store,'R1'))==1
    forged=deepcopy(saved);forged['public_source']={'provider':'made up'}
    assert store.save_sketch('R1',forged,1,'appraiser')['public_source']['provider']=='City of Lubbock public GIS'


def test_approval_requires_outline_scale_and_classification(store,monkeypatch):
    service(monkeypatch);saved=store.import_public_sketch('R1',pm.build('R1'),0,'actor')
    with pytest.raises(sketch.SketchError):store.save_sketch('R1',saved,1,'actor',True)
    saved['components'][0].update(kind='main',verified=True);saved['calibration']['verified']=True
    approved=store.save_sketch('R1',saved,1,'actor',True)
    assert approved['status']=='APPROVED' and approved['measurement_check'] is None


def test_endpoint_preview_has_no_writes_and_checks_district(store,monkeypatch):
    from fastapi import FastAPI,HTTPException
    from fastapi.testclient import TestClient
    from types import SimpleNamespace
    from app.commercial.api import commercial_router
    service(monkeypatch)
    monkeypatch.setattr('app.commercial.api.load_store',lambda *a:store)
    def resolve(token):
        if token!='ok':raise HTTPException(401,'Sign in')
        return SimpleNamespace(district_id='test',district_slug='lubbock',email='appraiser')
    app=FastAPI();app.include_router(commercial_router(resolve));client=TestClient(app)
    assert client.post('/api/commercial/sketch/auto-measure',json={}).status_code==401
    payload={'access_token':'ok','id':'R1','revision':0,'preview':True}
    response=client.post('/api/commercial/sketch/auto-measure',json=payload)
    assert response.status_code==200 and response.json()['summary']['components'][0]['square_feet']==5000
    assert sketch.get(store,'R1')['revision']==0 and not sketch.photos(store,'R1')
    payload['preview']=False
    assert client.post('/api/commercial/sketch/auto-measure',json=payload).status_code==200
    assert sketch.get(store,'R1')['revision']==1


def test_mismatched_link_and_unexpected_image_host_fail_closed(monkeypatch):
    original=service(monkeypatch)
    def wrong_pin(url,params=None,binary=False):
        data=original(url,params,binary)
        if url==pm.BUILDINGS:data['features'][0]['attributes']['PIN']='999'
        return data
    monkeypatch.setattr(pm,'fetch',wrong_pin)
    with pytest.raises(sketch.SketchError,match='does not match'):pm.build('R1')
    def unexpected_image(url,params=None,binary=False):
        data=original(url,params,binary)
        if url==pm.IMAGERY+'/exportImage':data['href']='https://example.org/image.jpg'
        return data
    monkeypatch.setattr(pm,'fetch',unexpected_image)
    with pytest.raises(sketch.SketchError,match='unexpected image'):pm.build('R1')


def test_non_lubbock_district_cannot_use_same_r_number(store,monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from types import SimpleNamespace
    from app.commercial.api import commercial_router
    monkeypatch.setattr('app.commercial.api.load_store',lambda *a:store)
    monkeypatch.setattr(pm,'build',lambda *a:pytest.fail('Must reject before public lookup'))
    app=FastAPI();app.include_router(commercial_router(lambda token:SimpleNamespace(district_id='other',district_slug='other-cad',email='actor')))
    response=TestClient(app).post('/api/commercial/sketch/auto-measure',json={'id':'R1','revision':0})
    assert response.status_code==400
    assert sketch.get(store,'R1')['revision']==0
