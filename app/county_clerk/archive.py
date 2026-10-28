"""Lossless private source archives in small JSON objects."""
import base64,gzip,hashlib,json
from io import BytesIO

MAX_BYTES=26214400
PART_CHARS=49152


def build(original,path):
    if len(original)<=65536:return original,[],{}
    encoded=base64.b64encode(gzip.compress(original,mtime=0)).decode()
    parts=[];manifest={'encoding':'gzip+base64','sha256':hashlib.sha256(original).hexdigest(),'parts':[]}
    for offset in range(0,len(encoded),PART_CHARS):
        part_path=path+'.parts/'+str(len(parts))+'.json'
        data=json.dumps({'data':encoded[offset:offset+PART_CHARS]}).encode()
        parts.append((part_path,data))
        manifest['parts'].append({'path':part_path,'sha256':hashlib.sha256(data).hexdigest()})
    return json.dumps(manifest).encode(),parts,{'archive_encoding':'gzip-base64-parts'}


def read(data,path,fetch):
    manifest=json.loads(data)
    if manifest.get('encoding')!='gzip+base64' or not 1<=len(manifest.get('parts',[]))<=1024:raise ValueError('Invalid source archive')
    encoded=[]
    for i,part in enumerate(manifest['parts']):
        if part['path']!=path+'.parts/'+str(i)+'.json':raise ValueError('Source archive path mismatch')
        content=fetch(part['path'])
        if hashlib.sha256(content).hexdigest()!=part['sha256']:raise ValueError('Source archive integrity failure')
        encoded.append(json.loads(content)['data'])
    compressed=base64.b64decode(''.join(encoded),validate=True)
    with gzip.GzipFile(fileobj=BytesIO(compressed)) as stream:original=stream.read(MAX_BYTES+1)
    if len(original)>MAX_BYTES or hashlib.sha256(original).hexdigest()!=manifest['sha256']:raise ValueError('Original source integrity failure')
    return original
