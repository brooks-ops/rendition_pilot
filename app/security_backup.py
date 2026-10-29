"""Encrypted logical backups. Only the public recovery key is deployed.

This supplements provider backups: it does not export auth password hashes,
PostgreSQL WAL, provider configuration, or a transaction-consistent pg_dump.
"""
import argparse
import base64
import hashlib
import io
import json
import os
import subprocess
import tempfile
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

BUCKET='security-backups'
PUBLIC_KEY=Path(__file__).resolve().parents[1]/'config/recovery-public.pem'
MAGIC=b'RPBACKUP1\n'


def config():
    url=os.environ['SUPABASE_URL'].rstrip('/')
    key=os.environ['SUPABASE_SERVICE_ROLE_KEY']
    return url, {'apikey':key,'Authorization':'Bearer '+key}


def request(method, path, **kwargs):
    url, headers=config()
    supplied_headers=kwargs.pop('headers',{})
    # Only idempotent operations are retried; object uploads use a unique name
    # and upsert so a dropped response cannot duplicate an archive part.
    retryable=method=='GET' or path.startswith('/storage/v1/object/list/') or (method=='POST' and supplied_headers.get('x-upsert')=='true') or method=='DELETE'
    for attempt in range(5 if retryable else 1):
        try:
            response=requests.request(method,url+path,headers={**headers,**supplied_headers},timeout=(10,120),**kwargs)
            if retryable and (response.status_code==429 or 500<=response.status_code<600) and attempt<4:
                time.sleep(min(8,0.5*2**attempt));continue
            response.raise_for_status()
            return response
        except (requests.ConnectionError,requests.Timeout):
            if not retryable or attempt==4:raise
            time.sleep(min(8,0.5*2**attempt))


def list_objects(bucket, prefix=''):
    offset=0
    while True:
        rows=request('POST','/storage/v1/object/list/'+quote(bucket,safe=''),json={'prefix':prefix,'limit':100,'offset':offset,'sortBy':{'column':'name','order':'asc'}}).json()
        for row in rows:
            name=prefix+row['name']
            if row.get('id') is None:
                yield from list_objects(bucket,name+'/')
            else:
                yield name
        if len(rows)<100:break
        offset+=len(rows)


def create_archive():
    manifest={'format':1,'started_at':datetime.now(timezone.utc).isoformat(),'tables':{},'objects':{},
              'limitations':['Logical, paginated exports are not a transaction-consistent PostgreSQL snapshot.',
                              'Auth passwords, WAL and provider configuration are excluded; preserve provider backups and migration sources.']}
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as archive:
        schema=request('GET','/rest/v1/').json()
        archive.writestr('postgrest-schema.json',json.dumps(schema))
        for table, definition in sorted(schema['definitions'].items()):
            # The API's table definitions enumerate the exposed public tables.
            primary=[name for name, prop in definition['properties'].items() if '<pk/>' in prop.get('description','')]
            order=','.join(primary or sorted(definition['properties']))
            offset=0;digest=hashlib.sha256()
            with archive.open('tables/'+table+'.jsonl','w') as output:
                while True:
                    response=request('GET','/rest/v1/'+quote(table,safe=''),params={'select':'*','order':order,'offset':offset,'limit':1000},headers={'Prefer':'count=exact'})
                    rows=response.json()
                    for row in rows:
                        line=(json.dumps(row,ensure_ascii=False,sort_keys=True,separators=(',',':'))+'\n').encode()
                        digest.update(line);output.write(line)
                    offset+=len(rows)
                    total=response.headers.get('Content-Range','').split('/')[-1]
                    if not rows or (total.isdigit() and offset>=int(total)):break
            manifest['tables'][table]={'rows':offset,'sha256':digest.hexdigest()}
        for bucket in request('GET','/storage/v1/bucket').json():
            if bucket['id']==BUCKET:continue  # Never recursively back up backups.
            if bucket.get('public'):raise RuntimeError('Unexpected public source bucket; review storage configuration.')
            for name in list_objects(bucket['id']):
                path=bucket['id']+'/'+name
                content=request('GET','/storage/v1/object/'+quote(path,safe='/')).content
                archive.writestr('objects/'+path,content)
                manifest['objects'][path]={'bytes':len(content),'sha256':hashlib.sha256(content).hexdigest()}
        manifest['finished_at']=datetime.now(timezone.utc).isoformat()
        archive.writestr('manifest.json',json.dumps(manifest,sort_keys=True))
    return buffer.getvalue(),manifest


def encrypt(archive, public_key=None):
    public=serialization.load_pem_public_key(public_key or PUBLIC_KEY.read_bytes())
    key=AESGCM.generate_key(bit_length=256);nonce=os.urandom(12)
    wrapped=public.encrypt(key,padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),algorithm=hashes.SHA256(),label=None))
    header=json.dumps({'wrapped_key':base64.b64encode(wrapped).decode(),'nonce':base64.b64encode(nonce).decode()},separators=(',',':')).encode()
    return MAGIC+len(header).to_bytes(4,'big')+header+AESGCM(key).encrypt(nonce,archive,header)


def decrypt(payload, private_key):
    if not payload.startswith(MAGIC):raise ValueError('Not a Rendition Pilot encrypted backup.')
    start=len(MAGIC);size=int.from_bytes(payload[start:start+4],'big');start+=4
    header=payload[start:start+size];metadata=json.loads(header)
    private=serialization.load_pem_private_key(private_key,password=None)
    key=private.decrypt(base64.b64decode(metadata['wrapped_key']),padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),algorithm=hashes.SHA256(),label=None))
    return AESGCM(key).decrypt(base64.b64decode(metadata['nonce']),payload[start+size:],header)


def verify(archive):
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        manifest=json.loads(bundle.read('manifest.json'))
        for table, meta in manifest['tables'].items():
            content=bundle.read('tables/'+table+'.jsonl')
            if hashlib.sha256(content).hexdigest()!=meta['sha256'] or len(content.splitlines())!=meta['rows']:
                raise ValueError('Table checksum/count mismatch: '+table)
            for row in content.splitlines():
                if not isinstance(json.loads(row),dict):raise ValueError('Invalid table row.')
        for path, meta in manifest['objects'].items():
            content=bundle.read('objects/'+path)
            if len(content)!=meta['bytes'] or hashlib.sha256(content).hexdigest()!=meta['sha256']:
                raise ValueError('Storage checksum mismatch: '+path)
    return manifest


def upload(encrypted, name=None):
    resume=name is not None
    if resume:datetime.strptime(name,'%Y%m%dT%H%M%SZ.rpbackup')
    name=name or datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'.rpbackup'
    parts=[]
    for index,start in enumerate(range(0,len(encrypted),256*1024)):
        part=encrypted[start:start+256*1024];path=name+f'.{index:04d}'
        digest=hashlib.sha256(part).hexdigest()
        if resume:
            try:
                existing=request('GET','/storage/v1/object/'+BUCKET+'/'+path).content
                if hashlib.sha256(existing).hexdigest()==digest:
                    parts.append({'path':path,'bytes':len(part),'sha256':digest});continue
            except requests.HTTPError as exc:
                if exc.response.status_code not in (400,404):raise
        request('POST','/storage/v1/object/'+BUCKET+'/'+path,data=part,headers={'Content-Type':'application/octet-stream','x-upsert':'true'})
        downloaded=request('GET','/storage/v1/object/'+BUCKET+'/'+path).content
        digest=hashlib.sha256(part).hexdigest()
        if hashlib.sha256(downloaded).hexdigest()!=digest:raise RuntimeError('Uploaded backup verification failed.')
        parts.append({'path':path,'bytes':len(part),'sha256':digest})
    descriptor={'format':1,'parts':parts,'sha256':hashlib.sha256(encrypted).hexdigest(),'bytes':len(encrypted)}
    # Write the completion marker only after every part has been read back.
    request('POST','/storage/v1/object/'+BUCKET+'/'+name+'.json',data=json.dumps(descriptor).encode(),headers={'Content-Type':'application/octet-stream','x-upsert':'true'})
    cutoff=datetime.now(timezone.utc)-timedelta(days=7)
    expired=[]
    for path in list_objects(BUCKET):
        try:recorded=datetime.strptime(path.split('.rpbackup')[0],'%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)
        except ValueError:continue
        if recorded<cutoff:expired.append(path)
    if expired:request('DELETE','/storage/v1/object/'+BUCKET,json={'prefixes':expired})
    return name+'.json'


def download(name):
    if '/' in name or not name.endswith('.rpbackup.json'):raise ValueError('Invalid backup descriptor name.')
    descriptor=request('GET','/storage/v1/object/'+BUCKET+'/'+name).json()
    parts=[]
    for part in descriptor['parts']:
        if not part['path'].startswith(name.removesuffix('.json')+'.') or '/' in part['path']:raise ValueError('Invalid backup part.')
        data=request('GET','/storage/v1/object/'+BUCKET+'/'+part['path']).content
        if len(data)!=part['bytes'] or hashlib.sha256(data).hexdigest()!=part['sha256']:raise ValueError('Backup part checksum mismatch.')
        parts.append(data)
    encrypted=b''.join(parts)
    if len(encrypted)!=descriptor['bytes'] or hashlib.sha256(encrypted).hexdigest()!=descriptor['sha256']:raise ValueError('Backup checksum mismatch.')
    return encrypted


def backup(local_output=None):
    archive, manifest=create_archive()
    verify(archive)
    encrypted=encrypt(archive)
    if local_output:
        target=Path(local_output);target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        target.write_bytes(encrypted);target.chmod(0o600)
    else:
        upload(encrypted)
    # Counts only: no taxpayer records, auth tokens, paths or keys in cron logs.
    print(json.dumps({'backup':'SUCCESS','destination':'local' if local_output else 'private cloud storage','tables':len(manifest['tables']),'rows':sum(v['rows'] for v in manifest['tables'].values()),'objects':len(manifest['objects']),'encrypted_bytes':len(encrypted),'finished_at':manifest['finished_at']}))
    return encrypted,manifest


def restore_drill(archive, database):
    """Restore every row to isolated PostgreSQL JSONB recovery tables; do not touch production.

    This tests data recoverability, not application schema/constraints/RLS restoration.
    """
    if not database.startswith('rendition_security_recovery_'):
        raise ValueError('Use a dedicated local rendition_security_recovery_* database.')
    manifest=verify(archive)
    sql=['BEGIN; CREATE SCHEMA recovered;']
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        for table, meta in manifest['tables'].items():
            if not table.replace('_','').isalnum():raise ValueError('Invalid table identifier.')
            sql.append('CREATE TABLE recovered."'+table+'" (row_data jsonb NOT NULL);')
            sql.append('COPY recovered."'+table+'" (row_data) FROM STDIN;')
            for line in bundle.read('tables/'+table+'.jsonl').decode().splitlines():
                sql.append(line.replace('\\','\\\\').replace('\t','\\t').replace('\r','\\r'))
            sql.append('\\.')
            sql.append("DO $$ BEGIN IF (SELECT count(*) FROM recovered.\""+table+"\") != "+str(meta['rows'])+" THEN RAISE EXCEPTION 'Recovery row count mismatch'; END IF; END $$;")
        sql.append('CREATE TABLE recovered.storage_objects(path text PRIMARY KEY, sha256 text, content bytea);')
        for path, meta in manifest['objects'].items():
            content=bundle.read('objects/'+path)
            escaped_path=path.replace("'","''")
            sql.append("INSERT INTO recovered.storage_objects VALUES ('"+escaped_path+"','"+meta['sha256']+"',decode('"+content.hex()+"','hex'));")
        sql.append('COMMIT;')
    subprocess.run(['psql','-h','/tmp','-d',database,'-v','ON_ERROR_STOP=1','-q'],input='\n'.join(sql)+'\n',text=True,check=True,stdout=subprocess.DEVNULL)
    return {'database':database,'tables_restored':len(manifest['tables']),'rows_restored':sum(v['rows'] for v in manifest['tables'].values()),'objects_restored':len(manifest['objects']),'mode':'isolated PostgreSQL JSONB logical-data recovery; not full application restore'}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output');parser.add_argument('--verify-file');parser.add_argument('--private-key');parser.add_argument('--restore-drill-db');args=parser.parse_args()
    if args.verify_file:
        archive=decrypt(Path(args.verify_file).read_bytes(),Path(args.private_key).read_bytes())
        manifest=verify(archive)
        result=restore_drill(archive,args.restore_drill_db) if args.restore_drill_db else {'verified':True,'tables':len(manifest['tables']),'objects':len(manifest['objects'])}
        print(json.dumps(result))
    else:backup(args.output)

if __name__=='__main__':main()
