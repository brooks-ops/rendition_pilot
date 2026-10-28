from __future__ import annotations
import os
import re
import threading
from pathlib import Path
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from app.activity_analytics import record_activity
from .store import Store,Conflict
from .remote import load_store


def commercial_router(resolve_context):
    router=APIRouter(prefix='/api/commercial');stores={};store_lock=threading.RLock()
    def context(payload):
        c=resolve_context(payload.get('access_token',''))
        district=c.district_id
        if not re.fullmatch(r'[a-zA-Z0-9_-]+',district):raise HTTPException(403,'Invalid district')
        directory=Path(os.environ.get('COMMERCIAL_DATA_DIR','commercial-data'))/district
        with store_lock:
            if district not in stores:stores[district]=load_store(directory,district)
        return stores[district],c

    @router.post('/catalog')
    def catalog(payload:dict):
        store,c=context(payload)
        return store.catalog(payload.get('filters'),max(0,int(payload.get('offset',0))),min(200,max(1,int(payload.get('limit',60)))))

    @router.post('/category-summary')
    def category_summary(payload:dict):
        store,c=context(payload)
        from .category_updates import category_summary
        try:return category_summary(store,payload.get('filters',{}))
        except (ValueError,KeyError) as exc:raise HTTPException(400,str(exc))

    @router.post('/property')
    def detail(payload:dict):
        store,c=context(payload)
        try:return store.detail(payload['id'])
        except KeyError:raise HTTPException(404,'Property row not found')

    @router.post('/equity')
    def equity(payload:dict):
        store,c=context(payload)
        from .equity import generate_equity
        try:
            result = generate_equity(store,payload['id'])
            record_activity(c, 'commercial_equity_generated', 'commercial', 'commercial_property', payload['id'])
            return result
        except KeyError:raise HTTPException(404,'Property row not found')

    @router.post('/preview')
    def preview(payload:dict):
        store,c=context(payload)
        try:return store.preview(payload)
        except (ValueError,KeyError) as exc:raise HTTPException(400,str(exc))

    @router.post('/apply')
    def apply(payload:dict):
        store,c=context(payload)
        try:
            result = store.commit(payload['token'],c.email,payload.get('reason',''))
            record_activity(c, 'commercial_update_applied', 'commercial', 'commercial_batch', result['batch_id'], units=result['updated'], dedup_key=result['batch_id'])
            return result
        except Conflict as exc:raise HTTPException(409,str(exc))
        except (ValueError,KeyError) as exc:raise HTTPException(400,str(exc))

    @router.post('/history')
    def history(payload:dict):
        store,c=context(payload);return {'items':store.history()}

    @router.post('/undo')
    def undo(payload:dict):
        store,c=context(payload)
        try:
            result = store.undo(payload['batch_id'],c.email)
            record_activity(c, 'commercial_update_undone', 'commercial', 'commercial_batch', payload['batch_id'], dedup_key=payload['batch_id'])
            return result
        except Conflict as exc:raise HTTPException(409,str(exc))

    @router.post('/create')
    def create(payload:dict):
        store,c=context(payload)
        try:
            result = store.create(payload['template_id'],payload.get('fields',{}),c.email)
            record_activity(c, 'commercial_account_created', 'commercial', 'commercial_property', result['id'], dedup_key=result['id'])
            return result
        except Conflict as exc:raise HTTPException(409,str(exc))
        except (ValueError,KeyError) as exc:raise HTTPException(400,str(exc))

    @router.post('/packet-list')
    def packet_list(payload:dict):
        store,c=context(payload)
        from .packet_import import preview_account_list
        try:return preview_account_list(store,payload)
        except (ValueError,TypeError) as exc:raise HTTPException(400,str(exc))

    @router.post('/packets/individual')
    def individual_packets(payload:dict):
        store,c=context(payload)
        from .packet_import import individual_packet_zip
        try:data=individual_packet_zip(store,payload.get('ids',[]),payload.get('notes',''))
        except (ValueError,TypeError,KeyError) as exc:raise HTTPException(400,str(exc))
        record_activity(c, 'commercial_report_generated', 'commercial', 'commercial_report_set', '|'.join(sorted(set(payload.get('ids',[])))) or 'report', units=len(set(payload.get('ids',[]))), outcome='INDIVIDUAL_PACKETS')
        return Response(data,media_type='application/zip',headers={'Content-Disposition':'attachment; filename="commercial-individual-packets.zip"'})

    @router.post('/packets')
    def packets(payload:dict):
        store,c=context(payload)
        from .packets import build_packet
        try:
            ids=payload.get('ids',[])
            data=build_packet(store,ids,payload.get('comparison_ids',[]),payload.get('notes',''))
            accounts={store.detail(rid)['account'].strip().upper() for rid in ids}
            import re
            account=next(iter(accounts)) if len(accounts)==1 else ''
            filename=account+'.pdf' if re.fullmatch(r'R[0-9]+',account) else 'commercial-appraisal-packets.pdf'
        except (ValueError,KeyError) as exc:raise HTTPException(400,str(exc))
        record_activity(c, 'commercial_report_generated', 'commercial', 'commercial_report_set', '|'.join(sorted(set(payload.get('ids',[])))) or 'report', units=len(set(payload.get('ids',[]))), outcome='COMBINED_PDF')
        return Response(data,media_type='application/pdf',headers={'Content-Disposition':f'attachment; filename="{filename}"'})
    def sketch_context(payload):
        store,c=context(payload)
        try: account=store.detail(payload['id'])['account']
        except KeyError:raise HTTPException(404,'Property row not found')
        return store,c,account

    def sketch_result(store,account,document=None):
        from . import sketch
        document=document or sketch.get(store,account)
        available=sketch.photos(store,account)
        return {'document':document,'photos':available,'summary':sketch.summary(document,available),
                'history':sketch.history(store,account),'ai_available':bool(os.getenv('COMMERCIAL_SKETCH_API_KEY') or os.getenv('OPENAI_API_KEY'))}

    @router.post('/sketch')
    def sketch_detail(payload:dict):
        store,c,account=sketch_context(payload)
        result=sketch_result(store,account)
        from . import sketch
        result['area_defaults']=sketch.area_defaults(store,store.detail(payload['id']))
        return result

    @router.post('/sketch/save')
    def sketch_save(payload:dict):
        from .sketch import SketchError,SketchConflict
        store,c,account=sketch_context(payload)
        try:
            revision=payload.get('revision')
            if isinstance(revision,bool) or not isinstance(revision,int) or revision<0:raise SketchError('Invalid sketch revision.')
            from . import sketch
            incoming=payload.get('document')
            if not isinstance(incoming,dict):raise SketchError('Invalid sketch document.')
            incoming={**incoming,'area_context':sketch.area_context(store.detail(payload['id']))}
            document=store.save_sketch(account,incoming,revision,c.email,payload.get('approve') is True)
        except SketchConflict as exc:raise HTTPException(409,str(exc))
        except SketchError as exc:raise HTTPException(400,str(exc))
        record_activity(c,'commercial_sketch_approved' if document['status']=='APPROVED' else 'commercial_sketch_saved','commercial','commercial_sketch',account,dedup_key=f"{account}:{document['revision']}")
        return sketch_result(store,account,document)

    @router.post('/sketch/photo')
    def sketch_photo(payload:dict):
        from .sketch import SketchError,SketchConflict
        store,c,account=sketch_context(payload)
        try:
            revision=payload.get('revision')
            if isinstance(revision,bool) or not isinstance(revision,int):raise SketchError('Invalid sketch revision.')
            document=store.upload_sketch_photo(account,payload.get('image'),payload.get('role'),payload.get('caption'),revision,c.email)
        except SketchConflict as exc:raise HTTPException(409,str(exc))
        except SketchError as exc:raise HTTPException(400,str(exc))
        record_activity(c,'commercial_sketch_photo_added','commercial','commercial_sketch',account,dedup_key=f"{account}:{document['revision']}")
        return sketch_result(store,account,document)

    @router.post('/sketch/generate')
    def sketch_generate(payload:dict):
        from . import sketch
        store,c,account=sketch_context(payload)
        document=sketch.get(store,account)
        if payload.get('revision')!=document['revision']:raise HTTPException(409,'Reload the latest sketch before generating an outline.')
        if document['components'] and payload.get('replace_existing') is not True:raise HTTPException(409,'Confirm replacement of existing components before generating.')
        try:
            draft=sketch.generate(document,sketch.photos(store,account))
            if not draft['components']:return {'no_outline':True,'warnings':draft['warnings'] or ['No reliable building outline was found. Try a clearer overhead or trace manually.']}
            if payload.get('preview') is True:
                return {**sketch_result(store,account), 'document':{**document,**draft},'preview':True,'summary':sketch.summary(draft,sketch.photos(store,account))}
            saved=store.save_sketch(account,draft,document['revision'],c.email,False)
        except sketch.SketchConflict as exc:raise HTTPException(409,str(exc))
        except sketch.SketchError as exc:raise HTTPException(400,str(exc))
        record_activity(c,'commercial_sketch_generated','commercial','commercial_sketch',account,dedup_key=f"{account}:{saved['revision']}")
        return sketch_result(store,account,saved)

    @router.post('/sketch/auto-measure')
    def public_auto_measure(payload:dict):
        from . import sketch,public_measure
        store,c,account=sketch_context(payload)
        district_slug=str(getattr(c,'district_slug','')).lower()
        if 'lubbock' not in district_slug:raise HTTPException(400,'Public auto-measure currently covers Lubbock accounts only.')
        document=sketch.get(store,account)
        revision=payload.get('revision')
        if type(revision) is not int or revision!=document['revision']:raise HTTPException(409,'Reload the latest sketch before measuring.')
        if document['components'] and payload.get('replace_existing') is not True:raise HTTPException(409,'Confirm replacement of existing components first.')
        try:
            bundle=public_measure.build(account)
            model_area=store.detail(payload['id'])['values'].get('area_sq_ft')
            if isinstance(model_area,(int,float)) and model_area>0:
                bundle['model_area_sq_ft']=model_area
                public_area=sum(f['square_feet'] for f in bundle['provenance']['footprints'])
                if abs(public_area/model_area-1)>.10:bundle['document']['warnings'].append('The GIS footprint differs from this model row area by more than 10%. They may represent different components; verify the building extent before approval.')
            if payload.get('preview') is True:
                return {**bundle,'summary':sketch.summary(bundle['document'],[bundle['photo']]),'preview':True}
            saved=store.import_public_sketch(account,bundle,revision,c.email,payload.get('replace_existing') is True)
        except sketch.SketchConflict as exc:raise HTTPException(409,str(exc))
        except sketch.SketchError as exc:raise HTTPException(400,str(exc))
        record_activity(c,'commercial_sketch_generated','commercial','commercial_sketch',account,dedup_key=f"{account}:{saved['revision']}")
        return sketch_result(store,account,saved)

    @router.post('/sketch/export')
    def sketch_export(payload:dict):
        from . import sketch
        store,c,account=sketch_context(payload)
        document=sketch.get(store,account);available=sketch.photos(store,account)
        if payload.get('format')=='xml':
            try:data=sketch.export_apex_xml(document,available)
            except sketch.SketchError as exc:raise HTTPException(400,str(exc))
            media='application/xml';extension='xml'
        elif payload.get('format')=='json':
            import json
            data=json.dumps({'format':'rendition-pilot-sketch-v1','document':document,'photos':[{k:v for k,v in p.items() if k!='data_url'} for p in available],'summary':sketch.summary(document,available)},indent=2).encode()
            media='application/json';extension='json'
        elif payload.get('format')=='pdf':data=sketch.export_pdf(document,available);media='application/pdf';extension='pdf'
        else:data=sketch.export_svg(document,available);media='image/svg+xml';extension='svg'
        filename=re.sub(r'[^a-zA-Z0-9_-]','_',account)+('.' if extension=='xml' else '-sketch.')+extension
        return Response(data,media_type=media,headers={'Content-Disposition':f'attachment; filename="{filename}"','Cache-Control':'no-store'})

    return router
