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
    return router
