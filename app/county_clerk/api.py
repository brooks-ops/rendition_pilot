import base64
from uuid import UUID
from fastapi import APIRouter,HTTPException,Response
from pydantic import BaseModel,Field
from app.comptroller.service import ComptrollerServiceError
from . import service
from .pdf_import import ImportError,MAX_BYTES

class Session(BaseModel):access_token:str
class Upload(Session):
    filename:str=Field(max_length=255)
    file_base64:str=Field(max_length=35000000)
class Commit(Upload):
    expected_sha256:str=Field(pattern=r'^[a-f0-9]{64}$')
    acknowledge_incomplete:bool=False
class Original(Session):import_id:UUID
class Filing(Session):filing_id:UUID

def router(require_bpp):
    api=APIRouter(prefix='/api/county-clerk')
    def data(request):
        try:raw=base64.b64decode(request.file_base64,validate=True)
        except Exception:raise HTTPException(400,'The uploaded PDF could not be decoded.')
        if len(raw)>MAX_BYTES:raise HTTPException(400,'Choose a PDF smaller than 25 MB.')
        return raw
    def run(function,*args,**kwargs):
        try:return function(*args,**kwargs)
        except ImportError as exc:raise HTTPException(400,str(exc))
        except (ComptrollerServiceError,service.requests.RequestException):raise HTTPException(503,'County Clerk storage is unavailable. Check the import history before retrying.')
    @api.post('/preview')
    def preview(request:Upload):
        district=require_bpp(request.access_token)
        return run(service.preview,district.district_id,data(request))
    @api.post('/import')
    def commit(request:Commit):
        district=require_bpp(request.access_token)
        return run(service.commit,district,data(request),request.filename,request.expected_sha256,request.acknowledge_incomplete)
    @api.post('/sync-status')
    def sync_status(request:Session):
        district=require_bpp(request.access_token)
        from .sync_status import dashboard
        return run(dashboard,district.district_id)
    @api.post('/history')
    def history(request:Session):
        district=require_bpp(request.access_token)
        try:return {'configured':True,**service.history(district.district_id)}
        except ImportError as exc:return {'configured':False,'message':str(exc)}
        except (ComptrollerServiceError,service.requests.RequestException):raise HTTPException(503,'County Clerk import history is unavailable.')
    @api.post('/reviewers')
    def reviewers(request:Session):
        district=require_bpp(request.access_token)
        rows=run(service.request,'GET','district_users',params={'select':'user_id,full_name,email','district_id':'eq.'+district.district_id,'active':'eq.true','limit':1000})
        return {'reviewers':[{'id':r['user_id'],'label':r.get('full_name') or r.get('email') or 'Appraiser'} for r in rows]}
    @api.post('/original')
    def original(request:Original):
        district=require_bpp(request.access_token)
        pdf,_=run(service.original,district.district_id,str(request.import_id))
        return Response(pdf,media_type='application/pdf',headers={'Content-Disposition':'attachment; filename="county-clerk-original.pdf"','Cache-Control':'no-store'})
    @api.post('/filing')
    def filing(request:Filing):
        district=require_bpp(request.access_token)
        rows=run(service.request,'GET','county_clerk_dba_filings',params={'select':'*','district_id':'eq.'+district.district_id,'id':'eq.'+str(request.filing_id),'limit':1})
        if not rows:raise HTTPException(404,'Filing not found in your district.')
        events=run(service.request,'GET','county_clerk_dba_review_events',params={'select':'id,actor_id,created_at,before_status:before_state->>status,after_status:after_state->>status,resolution:after_state->>resolution,notes:after_state->>resolution_notes,account:after_state->>matched_account_number','district_id':'eq.'+district.district_id,'filing_id':'eq.'+str(request.filing_id),'order':'created_at.desc','limit':50})
        return {'filing':rows[0],'audit':events,'notice':'Filing evidence alone does not establish that a business opened or closed.'}
    return api
