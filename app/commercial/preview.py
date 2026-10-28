"""Loopback-only preview. Production mounts the router with district auth."""
from types import SimpleNamespace
from pathlib import Path
from fastapi import FastAPI,Request,HTTPException
from fastapi.responses import FileResponse,JSONResponse
from .api import commercial_router

app=FastAPI(title='Commercial workspace local preview')
from fastapi.staticfiles import StaticFiles
app.mount('/frontend',StaticFiles(directory=Path(__file__).resolve().parents[2]/'frontend'),name='frontend')

@app.middleware('http')
async def loopback_only(request:Request,call_next):
    if request.client.host not in ('127.0.0.1','::1','testclient'):return JSONResponse({'detail':'Local preview only'},status_code=403)
    if request.method=='POST' and request.headers.get('origin'):
        if request.headers['origin'] not in ('http://127.0.0.1:8767','http://localhost:8767'):return JSONResponse({'detail':'Untrusted origin'},status_code=403)
    return await call_next(request)

app.include_router(commercial_router(lambda token:SimpleNamespace(district_id='lubbock-preview',district_slug='lubbock-preview',email='local-preview',role='admin')))

@app.get('/')
def index():return FileResponse(Path(__file__).resolve().parents[2]/'frontend'/'commercial.html')

@app.post('/api/commercial/session')
def session():return {'local_preview':True}
