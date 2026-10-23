from __future__ import annotations
import os
import re
from pathlib import Path
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from .store import Store,Conflict


def commercial_router(resolve_context):
    router=APIRouter(prefix='/api/commercial');stores={}
    def context(payload):
        c=resolve_context(payload.get('access_token',''))
        district=c.district_id
        if not re.fullmatch(r'[a-zA-Z0-9_-]+',district):raise HTTPException(403,'Invalid district')
        directory=Path(os.environ.get('COMMERCIAL_DATA_DIR','commercial-data'))/district
        if not (directory/'manifest.json').exists():raise HTTPException(409,'Commercial models have not been imported for your district.')
        if district not in stores:stores[district]=Store(directory)
        return stores[district],c

    @router.post('/catalog')
    def catalog(payload:dict):
        store,c=context(payload)
        return store.catalog(payload.get('filters'),max(0,int(payload.get('offset',0))),min(200,max(1,int(payload.get('limit',60)))))

    @router.post('/property')
    def detail(payload:dict):
        store,c=context(payload)
        try:return store.detail(payload['id'])
        except KeyError:raise HTTPException(404,'Property row not found')

    @router.post('/preview')
    def preview(payload:dict):
        store,c=context(payload)
        try:return store.preview(payload)
        except (ValueError,KeyError) as exc:raise HTTPException(400,str(exc))

    @router.post('/apply')
    def apply(payload:dict):
        store,c=context(payload)
        try:return store.commit(payload['token'],c.email,payload.get('reason',''))
        except Conflict as exc:raise HTTPException(409,str(exc))
        except (ValueError,KeyError) as exc:raise HTTPException(400,str(exc))

    @router.post('/history')
    def history(payload:dict):
        store,c=context(payload);return {'items':store.history()}

    @router.post('/undo')
    def undo(payload:dict):
        store,c=context(payload)
        try:return store.undo(payload['batch_id'],c.email)
        except Conflict as exc:raise HTTPException(409,str(exc))

    @router.post('/create')
    def create(payload:dict):
        store,c=context(payload)
        try:return store.create(payload['template_id'],payload.get('fields',{}),c.email)
        except Conflict as exc:raise HTTPException(409,str(exc))
        except (ValueError,KeyError) as exc:raise HTTPException(400,str(exc))

    @router.post('/packets')
    def packets(payload:dict):
        store,c=context(payload)
        from .packets import build_packet
        try:data=build_packet(store,payload.get('ids',[]),payload.get('comparison_ids',[]),payload.get('notes',''))
        except (ValueError,KeyError) as exc:raise HTTPException(400,str(exc))
        return Response(data,media_type='application/pdf',headers={'Content-Disposition':'attachment; filename="commercial-appraisal-packets.pdf"'})
    return router
