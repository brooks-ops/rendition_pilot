"""Private district-scoped storage for the single-instance Render deployment."""
import os
import sqlite3
import tempfile
import threading
from pathlib import Path
from urllib.parse import quote
import requests
from fastapi import HTTPException
from .store import Store

BUCKET='commercial-workspace'
_lock=threading.RLock()


def config():
    return os.environ.get('SUPABASE_URL','').rstrip('/'),os.environ.get('SUPABASE_SERVICE_ROLE_KEY','')


def object_request(method,district,name,data=None):
    url,key=config()
    path='/'.join(quote(part,safe='') for part in (district,name))
    return requests.request(method,f'{url}/storage/v1/object/{BUCKET}/{path}',headers={'apikey':key,'Authorization':'Bearer '+key,'x-upsert':'true','Content-Type':'application/octet-stream'},data=data,timeout=90)


def load_store(directory,district):
    _,key=config()
    if not key:
        if not (directory/'manifest.json').exists():raise HTTPException(409,'Commercial models have not been imported for your district.')
        return Store(directory)
    with _lock:
        directory.mkdir(parents=True,exist_ok=True)
        try:
            manifest=object_request('GET',district,'manifest.json')
            if manifest.status_code==404 or (manifest.status_code==400 and manifest.json().get('statusCode')=='404'):raise HTTPException(409,'Commercial models have not been imported for your district.')
            manifest.raise_for_status();metadata=manifest.json()
            names=list(metadata['graphs'].values())+['workspace.sqlite3']
            # Always restore the durable database on first load in this process.
            for name in names:
                if Path(name).name!=name:raise ValueError('Invalid commercial data filename')
                if name!='workspace.sqlite3' and (directory/name).exists():continue
                response=object_request('GET',district,name);response.raise_for_status()
                target=directory/name
                if name=='workspace.sqlite3':
                    for suffix in ('-wal','-shm'):Path(str(target)+suffix).unlink(missing_ok=True)
                temp=target.with_suffix(target.suffix+'.download');temp.write_bytes(response.content);temp.replace(target)
            (directory/'manifest.json').write_bytes(manifest.content)
            return RemoteStore(directory,district)
        except HTTPException:raise
        except (requests.RequestException,ValueError,KeyError) as exc:raise HTTPException(503,'Commercial storage is unavailable. Please try again.') from exc


class RemoteStore(Store):
    def __init__(self,directory,district):
        super().__init__(directory);self.remote_district=district;self.remote_lock=threading.RLock()

    def durable_change(self,operation,*args):
        with self.remote_lock,tempfile.TemporaryDirectory() as temp:
            backup=Path(temp)/'before.sqlite3'
            with self.connect() as source,sqlite3.connect(backup) as dest:source.backup(dest)
            result=operation(*args)
            snapshot=Path(temp)/'after.sqlite3'
            with self.connect() as source,sqlite3.connect(snapshot) as dest:source.backup(dest)
            try:object_request('POST',self.remote_district,'workspace.sqlite3',snapshot.read_bytes()).raise_for_status()
            except requests.RequestException as exc:
                with sqlite3.connect(backup) as source,self.connect() as dest:source.backup(dest)
                self._equity_pool=None
                raise HTTPException(503,'The change could not be saved to durable storage and was rolled back. Please try again.') from exc
            return result

    def commit(self,*args):return self.durable_change(super().commit,*args)
    def undo(self,*args):return self.durable_change(super().undo,*args)
    def create(self,*args):return self.durable_change(super().create,*args)
