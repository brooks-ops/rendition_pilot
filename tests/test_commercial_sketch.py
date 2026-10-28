import base64
import io
import json
from types import SimpleNamespace
import pytest
from PIL import Image
from app.commercial import sketch
from tests.test_commercial_workspace import store


def rectangle():
    return dict(components=[dict(id='main',label='Main building',kind='main',points=[[0,0],[100,0],[100,100],[0,100]],source='manual',verified=True)],calibration=dict(a=[0,0],b=[100,0],feet=10,verified=True),overhead_id=None,notes='Field measurement',warnings=[])


def image():
    out=io.BytesIO();Image.new('RGB',(1000,500),'white').save(out,'JPEG');return 'data:image/jpeg;base64,'+base64.b64encode(out.getvalue()).decode()


def test_sketch_save_approve_shared_account_and_revision_conflict(store):
    saved=store.save_sketch('R1',rectangle(),0,'verified@cad.test',True)
    assert saved['status']=='APPROVED' and saved['approved_by']=='verified@cad.test'
    assert sketch.get(store,'R1')['revision']==1
    with pytest.raises(sketch.SketchConflict):store.save_sketch('R1',rectangle(),0,'other@cad.test')
    assert sketch.get(store,'R1')['revision']==1
    edit=rectangle();edit['notes']='new inspection'
    revised=store.save_sketch('R1',edit,1,'other@cad.test')
    assert revised['status']=='DRAFT' and revised['approved_by'] is None
    assert len(sketch.history(store,'R1'))==2
    assert sketch.get(store,'R2')['revision']==0


def test_invalid_geometry_and_unverified_approval_do_not_write(store):
    bad=rectangle();bad['components'][0]['points']=[[0,0],[100,100],[0,100],[100,0]]
    with pytest.raises(sketch.SketchError):store.save_sketch('R1',bad,0,'actor')
    bad=rectangle();bad['components'][0]['points'][0]=[float('nan'),0]
    with pytest.raises(sketch.SketchError):store.save_sketch('R1',bad,0,'actor')
    for field in ('component','calibration'):
        doc=rectangle()
        if field=='component':doc['components'][0]['verified']=False
        else:doc['calibration']['verified']=False
        with pytest.raises(sketch.SketchError):store.save_sketch('R1',doc,0,'actor',True)
    assert sketch.get(store,'R1')['revision']==0


def test_area_respects_image_aspect_ratio_and_scale(store):
    doc=rectangle()
    # Blank canvas is 1000 x 700. A 10ft horizontal edge leaves a 7ft vertical edge.
    sizes=sketch.summary(doc,[])
    assert sizes['components'][0]['square_feet']==70
    assert sizes['components'][0]['wall_feet']==[10,7,10,7]
    doc['calibration']=None
    assert sketch.summary(doc,[])['components'][0]['square_feet'] is None


def test_photo_upload_validated_scoped_and_preserves_geometry(store):
    doc=store.upload_sketch_photo('R1',image(),'overhead','Aerial',0,'actor')
    photos=sketch.photos(store,'R1')
    assert len(photos)==1 and photos[0]['width']==1000 and photos[0]['height']==500
    assert sketch.photos(store,'R2')==[]
    assert doc['overhead_id']==photos[0]['id']
    drawn=rectangle();drawn['overhead_id']=doc['overhead_id']
    store.save_sketch('R1',drawn,1,'actor')
    with pytest.raises(sketch.SketchError):store.upload_sketch_photo('R1',image(),'overhead','Replacement',2,'actor')
    added=store.upload_sketch_photo('R1',image(),'side','Side photo',2,'actor')
    assert len(added['components'])==1 and added['status']=='DRAFT'
    assert len(sketch.photos(store,'R1'))==2
    with pytest.raises(sketch.SketchError):store.upload_sketch_photo('R2','not an image','front','bad',0,'actor')
    assert sketch.get(store,'R2')['revision']==0


def test_ai_missing_configuration_never_fabricates_geometry(monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY',raising=False);monkeypatch.delenv('COMMERCIAL_SKETCH_API_KEY',raising=False)
    with pytest.raises(sketch.SketchError,match='credential'):sketch.generate(sketch.blank('R1'),[])


def test_ai_output_is_unverified_unscaled_and_validated(monkeypatch):
    from app.commercial import sketch_ai as ai
    available=[dict(id='overhead',role='overhead',width=1000,height=500,data_url=image())]
    available += [dict(id=f'old-{i}',role='detail',data_url=f'data:old-{i}') for i in range(6)]
    available += [dict(id='front-new',role='front',data_url='data:front-new'),dict(id='side-new',role='side',data_url='data:side-new')]
    focus=ai.Focus(suitable_overhead=True,projection='near_vertical',target_unambiguous=True,left=100,top=100,right=800,bottom=400,visible_account_ids=[],warnings=[])
    trace=ai.Trace(components=[ai.Component(label='Main',kind='main',points=[ai.PixelPoint(x=x,y=y) for x,y in [[10,10],[200,10],[200,200],[10,200]]],edge_basis='roof_edge',evidence='Visible roof perimeter')],warnings=['Roof edge uncertain'])
    review=ai.Review(acceptable_visual_alignment=True,covers_target=True,unsupported_components=False,issues=[])
    responses={ai.Focus:focus,ai.Trace:trace,ai.Review:review};calls=[]
    class FakeClient:
        def __init__(self,**kwargs):self.responses=self
        def parse(self,**kwargs):calls.append(kwargs);return SimpleNamespace(output_parsed=responses[kwargs['text_format']])
    monkeypatch.setenv('COMMERCIAL_SKETCH_API_KEY','test-only');monkeypatch.setattr('openai.OpenAI',FakeClient)
    doc=sketch.blank('R1');doc['overhead_id']='overhead'
    generated=sketch.generate(doc,available)
    assert generated['components'][0]['source']=='ai'
    assert not generated['components'][0]['verified'] and generated['calibration'] is None
    assert generated['components'][0]['points'][0]==[68,136]  # Crop origin mapped back to full-image coordinates.
    assert len(calls)==4 and all(call['store'] is False for call in calls)
    images=[part['image_url'] for part in calls[0]['input'][0]['content'] if part['type']=='input_image']
    assert images[1:3]==['data:front-new','data:side-new']
    assert len(images)==4
    assert generated['generation_quality']['survey_verified'] is False
    trace.components[0].points[0].x=1001
    with pytest.raises(sketch.SketchError):sketch.generate(doc,available)


def test_svg_export_escapes_labels_and_marks_drafts(store):
    doc=store.save_sketch('R1',rectangle(),0,'actor')
    doc['components'][0]['label']='<script>alert(1)</script>'
    output=sketch.export_svg(doc,[]).decode()
    assert '<script>' not in output and '&lt;script&gt;' in output
    assert 'DRAFT' in output and '70.0 SF' in output


def test_api_uses_server_account_and_actor_and_cannot_approve_unverified(store,monkeypatch):
    import importlib
    from fastapi import FastAPI,HTTPException
    from fastapi.testclient import TestClient
    api=importlib.import_module('app.commercial.api')
    monkeypatch.setattr(api,'load_store',lambda *a:store)
    monkeypatch.setattr(api,'record_activity',lambda *a,**kw:None)
    def context(token):
        if token!='allowed':raise HTTPException(403,'No Commercial access')
        return SimpleNamespace(district_id='d1',email='real@cad.test',user_id='u1')
    app=FastAPI();app.include_router(api.commercial_router(context));client=TestClient(app)
    rid=store.manifest['records'][0]['id'];account=store.detail(rid)['account']
    body=dict(access_token='allowed',id=rid,account='spoofed-account',actor='spoofed',document=rectangle(),revision=0)
    assert client.post('/api/commercial/sketch/save',json={**body,'access_token':'denied'}).status_code==403
    assert client.post('/api/commercial/sketch/save',json={**body,'id':'not-in-district'}).status_code==404
    result=client.post('/api/commercial/sketch/save',json=body)
    assert result.status_code==200
    assert result.json()['document']['account']==account
    assert result.json()['document']['updated_by']=='real@cad.test'
    assert sketch.get(store,'spoofed-account')['revision']==0
    assert client.post('/api/commercial/sketch/save',json=body).status_code==409
    bad=rectangle();bad['components'][0]['verified']=False
    assert client.post('/api/commercial/sketch/save',json={**body,'revision':1,'approve':True,'document':bad}).status_code==400


def test_remote_sketch_write_rolls_back_photo_and_revision_when_storage_fails(store,monkeypatch):
    from app.commercial.remote import RemoteStore
    from fastapi import HTTPException
    remote=RemoteStore(store.directory,'district-one')
    class Failed:
        def raise_for_status(self):raise RuntimeError('storage offline')
    monkeypatch.setattr('app.commercial.remote.object_request',lambda *a,**kw:Failed())
    with pytest.raises(HTTPException) as failure:remote.save_sketch('R1',rectangle(),0,'actor')
    assert failure.value.status_code==503
    assert sketch.get(remote,'R1')['revision']==0
    assert sketch.history(remote,'R1')==[]
    with pytest.raises(HTTPException):remote.upload_sketch_photo('R1',image(),'overhead','Overhead',0,'actor')
    assert sketch.photos(remote,'R1')==[]
    assert sketch.get(remote,'R1')['revision']==0


def test_sketch_persistence_survives_store_reload_and_does_not_change_valuation(store):
    from app.commercial.store import Store
    before=store.detail('R1')['values']
    store.save_sketch('R1',rectangle(),0,'actor',True)
    reloaded=Store(store.directory)
    assert sketch.get(reloaded,'R1')['status']=='APPROVED'
    assert reloaded.detail('R1')['values']==before


def test_side_photo_cannot_be_used_as_overhead_and_unbounded_scale_is_rejected(store):
    doc=rectangle();doc['overhead_id']='photo'
    with pytest.raises(sketch.SketchError):sketch.validate(doc,[{'id':'photo','role':'side'}])
    doc=rectangle();doc['calibration']['feet']=float('inf')
    with pytest.raises(sketch.SketchError):sketch.validate(doc,[])


def test_pdf_export_contains_account_scale_approval_and_component_area(store):
    import fitz
    doc=store.save_sketch('R1',rectangle(),0,'appraiser@cad.test',True)
    pdf=fitz.open(stream=sketch.export_pdf(doc,[]),filetype='pdf')
    text=' '.join(page.get_text() for page in pdf)
    assert 'R1 - Building sketch' in text
    assert 'APPROVED' in text and '70.0 SF' in text
    assert 'Verified scale' in text and '10.0 ft' in text


def test_remote_photo_objects_keep_workspace_snapshot_small(store,monkeypatch):
    from app.commercial.remote import RemoteStore
    remote=RemoteStore(store.directory,'district-one');objects={}
    class Result:
        def __init__(self,content=b''):self.content=content
        def raise_for_status(self):pass
    def request(method,district,name,data=None):
        assert district=='district-one'
        if method=='POST':objects[name]=data;return Result()
        return Result(objects[name])
    monkeypatch.setattr('app.commercial.remote.object_request',request)
    saved=remote.upload_sketch_photo('R1',image(),'overhead','Overhead',0,'actor')
    assert saved['revision']==1
    with remote.connect() as db:reference=db.execute('SELECT image FROM building_sketch_photos').fetchone()[0]
    assert reference.startswith('remote:sketch-photo-')
    assert objects['workspace.sqlite3']
    assert sketch.photos(remote,'R1')[0]['data_url'].startswith('data:image/jpeg;base64,')


def test_ai_approval_requires_a_different_direction_and_agreeing_measurement(store):
    doc=rectangle();doc['components'][0]['source']='ai'
    with pytest.raises(sketch.SketchError,match='second'):store.save_sketch('R1',doc,0,'actor',True)
    doc['measurement_check']=dict(a=[0,0],b=[100,0],feet=10,verified=True)
    with pytest.raises(sketch.SketchError,match='different direction'):store.save_sketch('R1',doc,0,'actor',True)
    doc['measurement_check']=dict(a=[100,0],b=[100,100],feet=10,verified=True)
    with pytest.raises(sketch.SketchError,match='Proportions'):store.save_sketch('R1',doc,0,'actor',True)
    doc['measurement_check']['feet']=7  # Blank canvas is 1000x700; normalized squares are not pixel squares.
    saved=store.save_sketch('R1',doc,0,'actor',True)
    assert saved['status']=='APPROVED'
    assert sketch.summary(saved,[])['measurement_check']['passed']
    doc['components'][0]['points'][2]=[100,110]
    with pytest.raises(sketch.SketchError,match='wall changed'):store.save_sketch('R1',doc,1,'actor',True)


def test_overlapping_components_cannot_be_approved_but_shared_walls_can(store):
    from copy import deepcopy
    doc=rectangle();second=deepcopy(doc['components'][0]);second['id']='addition';doc['components'].append(second)
    with pytest.raises(sketch.SketchError,match='overlap'):store.save_sketch('R1',doc,0,'actor',True)
    second['points']=[[100,0],[200,0],[200,100],[100,100]]
    assert store.save_sketch('R1',doc,0,'actor',True)['status']=='APPROVED'


def test_failed_visual_check_returns_no_replacement(monkeypatch):
    from app.commercial import sketch_ai as ai
    from copy import deepcopy
    focus=ai.Focus(suitable_overhead=True,projection='near_vertical',target_unambiguous=True,left=100,top=100,right=800,bottom=400,visible_account_ids=[],warnings=[])
    trace=ai.Trace(components=[ai.Component(label='Main',kind='main',points=[ai.PixelPoint(x=x,y=y) for x,y in [[10,10],[200,10],[200,200],[10,200]]],edge_basis='roof_edge',evidence='Roof')],warnings=[])
    review=ai.Review(acceptable_visual_alignment=False,covers_target=False,unsupported_components=True,issues=['Outline runs through the parking lot.'])
    monkeypatch.setenv('COMMERCIAL_SKETCH_API_KEY','test')
    monkeypatch.setattr(ai,'_ask',lambda client,model,schema,*a:{ai.Focus:focus,ai.Trace:trace,ai.Review:review}[schema])
    doc={**sketch.blank('R1'),**rectangle(),'overhead_id':'overhead'};before=deepcopy(doc)
    result=sketch.generate(doc,[dict(id='overhead',role='overhead',width=1000,height=500,data_url=image())])
    assert result['components']==[] and any('parking lot' in w for w in result['warnings'])
    assert doc==before


def test_oblique_overhead_is_rejected_before_tracing(monkeypatch):
    from app.commercial import sketch_ai as ai
    calls=[]
    def ask(client,model,schema,*a):
        calls.append(schema)
        return ai.Focus(suitable_overhead=False,projection='oblique',target_unambiguous=True,left=0,top=0,right=100,bottom=100,visible_account_ids=[],warnings=[])
    monkeypatch.setenv('COMMERCIAL_SKETCH_API_KEY','test');monkeypatch.setattr(ai,'_ask',ask)
    doc=sketch.blank('R1');doc['overhead_id']='overhead'
    assert not sketch.generate(doc,[dict(id='overhead',role='overhead',data_url=image())])['components']
    assert calls==[ai.Focus]


def test_preview_generation_never_saves_over_current_sketch(store,monkeypatch):
    import importlib
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    api=importlib.import_module('app.commercial.api')
    monkeypatch.setattr(api,'load_store',lambda *a:store)
    monkeypatch.setattr(api,'record_activity',lambda *a,**kw:None)
    monkeypatch.setattr(sketch,'generate',lambda *a:rectangle())
    app=FastAPI();app.include_router(api.commercial_router(lambda token:SimpleNamespace(district_id='d1',email='actor',user_id='u1')))
    client=TestClient(app);rid=store.manifest['records'][0]['id'];account=store.detail(rid)['account']
    response=client.post('/api/commercial/sketch/generate',json={'id':rid,'revision':0,'preview':True})
    assert response.status_code==200 and response.json()['preview'] is True
    assert sketch.get(store,account)['revision']==0 and sketch.history(store,account)==[]


def test_two_visually_accepted_real_attempts_still_fail_repeatability():
    from app.commercial.sketch_ai import outline_agreement
    # Pixel-normalized traces of the reported case differ at the front edge.
    a=[{'kind':'main','points':[[319.8198,340.1015],[382.8829,341.7936],[384.3844,309.6447],[767.2673,319.797],[761.2613,597.2927],[322.8228,587.1404],[322.8228,531.3029],[280.7808,529.6108],[283.7838,421.3198],[319.8198,421.3198]]}]
    b=[{'kind':'main','points':[[384.3844,307.9526],[768.7688,319.797],[759.7598,632.8257],[312.3123,619.2893],[315.3153,531.3029],[280.7808,529.6108],[283.7838,419.6277],[318.3183,419.6277],[321.3213,338.4095],[382.8829,340.1015]]}]
    check=outline_agreement(a,b,(666,591))
    assert not check['passed'] and check['minimum_iou']<.90
    assert check['maximum_boundary_difference_pixels']>15


def test_agreement_checks_component_types_and_not_only_union_area():
    from app.commercial.sketch_ai import outline_agreement
    from copy import deepcopy
    a=rectangle()['components'];b=deepcopy(a)
    assert outline_agreement(a,b,(1000,700))['passed']
    b[0]['kind']='canopy'
    assert not outline_agreement(a,b,(1000,700))['passed']
