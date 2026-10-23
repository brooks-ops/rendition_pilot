"""Read account rows and formula provenance directly from the supplied ZIP."""
from __future__ import annotations
import gzip
import hashlib
import io
import json
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from openpyxl.formula.translate import Translator

NS={'m':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
RID='{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'
DEFINITIONS=json.loads(Path(__file__).with_name('model_definitions.json').read_text())


def read_sheet(z, path, strings):
    cells={};shared={}
    with z.open(path) as stream:
        for _,row in ET.iterparse(stream,events=['end']):
            if row.tag!='{'+NS['m']+'}row':continue
            for cell in row.findall('m:c',NS):
                addr=cell.attrib['r'];kind=cell.attrib.get('t')
                value=cell.findtext('m:v',default=None,namespaces=NS)
                if kind=='s':value=strings[int(value)] if value is not None else None
                elif kind=='inlineStr':value=''.join(cell.find('m:is',NS).itertext())
                elif kind=='b':value=value=='1'
                elif kind not in ('str','e') and value is not None:
                    try:value=float(value);value=int(value) if value.is_integer() else value
                    except ValueError:pass
                f=cell.find('m:f',NS);formula=None
                if f is not None:
                    formula=f.text
                    if f.attrib.get('t')=='shared':
                        si=f.attrib['si']
                        if formula:shared[si]=(addr,formula)
                        elif si in shared:
                            origin,text=shared[si]
                            formula=Translator('='+text,origin=origin).translate_formula(addr).lstrip('=')
                if value is not None or formula:
                    cells[addr]={'v':value,**({'f':formula} if formula else {}),**({'error':True} if kind=='e' else {})}
            row.clear()
    return cells


def import_zip(source: Path, directory: Path):
    directory.mkdir(parents=True,exist_ok=True)
    graphs={};records=[];skipped=[]
    digest=hashlib.sha256(source.read_bytes()).hexdigest()
    with zipfile.ZipFile(source) as outer:
        for definition in DEFINITIONS:
            name=next((n for n in outer.namelist() if Path(n).name==definition['workbook']),None)
            if not name:raise ValueError('Missing model: '+definition['workbook'])
            model=definition['model'];sheets={}
            with zipfile.ZipFile(io.BytesIO(outer.read(name))) as z:
                strings=[]
                if 'xl/sharedStrings.xml' in z.namelist():
                    strings=[''.join(s.itertext()) for s in ET.fromstring(z.read('xl/sharedStrings.xml'))]
                rels={r.attrib['Id']:r.attrib['Target'] for r in ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))}
                for sh in ET.fromstring(z.read('xl/workbook.xml')).findall('m:sheets/m:sheet',NS):
                    target=rels[sh.attrib[RID]]
                    path=target.lstrip('/') if target.startswith('/') else posixpath.normpath('xl/'+target)
                    if z.getinfo(path).file_size>100_000_000:
                        skipped.append({'model':model,'sheet':sh.attrib['name'],'reason':'Oversized historical sheet; not used as the account catalog'})
                        continue
                    sheets[sh.attrib['name']]=read_sheet(z,path,strings)
            graph_path=directory/(model+'.json.gz')
            with gzip.open(graph_path,'wt',encoding='utf-8') as stream:json.dump(sheets,stream,separators=(',',':'))
            graphs[model]=graph_path.name
            main=sheets[definition['sheet']];account_col=definition['fields']['account']
            for addr,cell in main.items():
                if not re.fullmatch(account_col+r'\d+',addr):continue
                account=str(cell.get('v') or '').strip().upper()
                if not re.fullmatch(r'R\d+',account):continue
                row=int(re.search(r'\d+',addr)[0])
                record={'id':hashlib.sha256(f'{model}/{row}'.encode()).hexdigest()[:24], 'model':model,'account':account,'source_row':row,'source_sheet':definition['sheet'],'source_file':definition['workbook'],'source_hash':digest}
                records.append(record)
            print(f'Imported {model}: {sum(r["model"]==model for r in records)} source rows',flush=True)
    manifest={'source_hash':digest,'source_name':source.name,'graphs':graphs,'models':DEFINITIONS,'skipped':skipped,'records':records}
    (directory/'manifest.json').write_text(json.dumps(manifest,separators=(',',':')))
    return manifest


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description='Import commercial model workbooks into a district workspace')
    parser.add_argument('source',type=Path)
    parser.add_argument('--data-dir',type=Path,required=True)
    parser.add_argument('--district',required=True)
    args=parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_-]+',args.district):parser.error('Invalid district identifier')
    directory=args.data_dir/args.district
    if (directory/'manifest.json').exists():parser.error('This district already has imported models. Use a new directory to stage another source version.')
    import_zip(args.source,directory)
