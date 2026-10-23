from __future__ import annotations
import gzip
import json
import math
import re
import sqlite3
import threading
import uuid
from datetime import datetime,timezone
from pathlib import Path
from openpyxl.formula import Tokenizer
from .formulas import Engine, FormulaError

LABELS={'cost':'Cost & equity','apartments':'Apartments','retail':'Retail','senior_housing':'Senior housing','supermarkets':'Supermarkets','pharmacies':'Pharmacies','hotels':'Hotels','miniwarehouse_storage':'Warehouse & storage','office':'Office'}
OUTPUTS={'noi','loaded_cap_rate','indicated_value','total_value','unit_value','effective_gross_income','projected_gross_rent','vacancy_loss','effective_gross_rent','other_income_amount','total_expenses','reserve_amount','bad_debt_amount','actual_noi','actual_indicated_value'}
TEXT_FIELDS={'account','name','address','owner','classification','category','quality','service_type'}
DISPLAY_LABELS={'projected_gross_rent':'Projected gross rent','vacancy_loss':'Vacancy loss','effective_gross_rent':'Effective gross rent','other_income_amount':'Other income','bad_debt_amount':'Bad debt','effective_gross_income':'Effective gross income','total_expenses':'Operating expenses','reserve_amount':'Reserves','noi':'Net operating income','cap_rate':'Base capitalization rate','tax_rate':'Tax rate','loaded_cap_rate':'Loaded capitalization rate','indicated_value':'Indicated income value','total_value':'Final model value','land_value':'Land value','improvement_value':'Improvement value','area_sq_ft':'Building area (sq ft)','unit_value':'Source price per sq ft','other_improvements':'Other improvements','other_land':'Other land','other_adjustments':'Other adjustments','land_adjustment':'Land adjustment'}


class Conflict(ValueError):pass


class Store:
    def __init__(self,directory):
        self.directory=Path(directory)
        self.manifest=json.loads((self.directory/'manifest.json').read_text())
        self.definitions={m['model']:m for m in self.manifest['models']}
        self.graphs={}
        self.lock=threading.RLock()
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY, metadata TEXT NOT NULL, overrides TEXT NOT NULL DEFAULT '{}', extra_cells TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL);
                INSERT OR IGNORE INTO state VALUES(1,0);
                CREATE TABLE IF NOT EXISTS previews(id TEXT PRIMARY KEY, revision INTEGER, payload TEXT, created_at TEXT);
                CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY, actor TEXT, reason TEXT, created_at TEXT, changes TEXT, undone INTEGER NOT NULL DEFAULT 0);
            ''')
            for record in self.manifest['records']:
                db.execute('INSERT OR IGNORE INTO records(id,metadata) VALUES(?,?)',(record['id'],json.dumps(record)))
            for raw in db.execute('SELECT id,metadata FROM records').fetchall():
                meta=json.loads(raw['metadata'])
                if 'reference_only' not in meta and not meta.get('new_property'):
                    spec=self.definitions[meta['model']]['fields'];cells=self.graph(meta['model'])[meta['source_sheet']]
                    meta['reference_only']=not any(cells.get(f'{col}{meta["source_row"]}',{}).get('v') not in (None,'') for field,col in spec.items() if field not in ('type','account'))
                    db.execute('UPDATE records SET metadata=? WHERE id=?',(json.dumps(meta),raw['id']))

    def connect(self):
        db=sqlite3.connect(self.directory/'workspace.sqlite3',timeout=30)
        db.row_factory=sqlite3.Row
        return db

    def graph(self,model):
        with self.lock:
            if model not in self.graphs:
                with gzip.open(self.directory/self.manifest['graphs'][model],'rt') as stream:self.graphs[model]=json.load(stream)
                if model=='cost':
                    cells=self.graphs[model][self.definitions[model]['sheet']]
                    for r in self.manifest['records']:
                        if r['model']!=model:continue
                        row=r['source_row'];land=cells.get(f'G{row}',{}).get('v');imp=cells.get(f'I{row}',{}).get('v');total=cells.get(f'K{row}',{}).get('v')
                        if all(isinstance(v,(int,float)) for v in (land,imp,total)) and abs(land+imp-total)<.01:
                            cells[f'K{row}']={**cells[f'K{row}'],'f':f'G{row}+I{row}','workspace_formula':True}
            while len(self.graphs)>2:
                self.graphs.pop(next(iter(self.graphs)))
            return self.graphs[model]

    def records(self,db=None):
        if db is None:
            with self.connect() as conn:return self.records(conn)
        return [{**json.loads(r['metadata']),'overrides':json.loads(r['overrides']),'extra_cells':json.loads(r['extra_cells'])} for r in db.execute('SELECT * FROM records')]

    def effective_engine(self,model,records,changes=None):
        sheets=dict(self.graph(model));sheet=self.definitions[model]['sheet'];main=None;overrides={}
        for r in records:
            if r['model']!=model:continue
            if r['extra_cells']:
                if main is None:main=dict(sheets[sheet]);sheets[sheet]=main
                main.update(r['extra_cells'])
            for col,v in r['overrides'].items():overrides[(sheet,f'{col}{r["source_row"]}')]=v
        for record,col,value in changes or []:overrides[(sheet,f'{col}{record["source_row"]}')]=value
        return Engine(sheets,overrides)

    def snapshot(self,r,engine=None,details=False):
        spec=self.definitions[r['model']];sheet=r['source_sheet'];row=r['source_row']
        cells=r['extra_cells'] or self.graph(r['model'])[sheet]
        values={};errors={}
        for field,col in spec['fields'].items():
            if field=='type':continue
            addr=f'{col}{row}';cell=cells.get(addr,{})
            try:
                if engine is not None:values[field]=engine.cell(sheet,addr)
                elif col in r['overrides']:values[field]=r['overrides'][col]
                elif cell.get('error'):raise FormulaError(str(cell.get('v')))
                else:values[field]=cell.get('v')
            except (FormulaError,ValueError,IndexError,TypeError) as exc:values[field]=None;errors[field]=str(exc)
        if engine is None and r.get('calculated'):
            values.update(r['calculated']['values']);errors=dict(r['calculated']['errors'])
        values['account']=r['account']
        result={k:v for k,v in r.items() if k not in ('extra_cells','overrides','calculated')}
        result.update({'values':values,'errors':errors,'override_fields':[f for f,c in spec['fields'].items() if c in r['overrides']],'label':LABELS[r['model']]})
        if details:
            result['fields']=[{'key':f,'column':c,'label':DISPLAY_LABELS.get(f,str(self.graph(r['model'])[sheet].get(c+'1',{}).get('v') or f.replace('_',' ').title())),'source_label':str(self.graph(r['model'])[sheet].get(c+'1',{}).get('v') or ''),'value':values.get(f),'editable':self.editable(r['model'],f),'formula':cells.get(f'{c}{row}',{}).get('f'),'source_cell':f'{c}{row}','error':errors.get(f)} for f,c in spec['fields'].items() if f!='type']
        return result

    def editable(self,model,field):
        return field not in OUTPUTS and field not in {'type','account'} and not (model=='apartments' and field=='units')

    def match(self,snapshot,filters):
        v=snapshot['values']
        if filters.get('model') and snapshot['model']!=filters['model']:return False
        if filters.get('category') and str(v.get('category') or '')!=filters['category']:return False
        if filters.get('classification') and str(v.get('classification') or '')!=filters['classification']:return False
        if filters.get('quality') and str(v.get('quality') or '')!=filters['quality']:return False
        if 'ids' in filters and snapshot['id'] not in filters['ids']:return False
        if filters.get('query'):
            hay=' '.join(str(v.get(k) or '') for k in ['account','name','address','owner','category'])
            if filters['query'].casefold() not in hay.casefold():return False
        area=v.get('area_sq_ft') if v.get('area_sq_ft') is not None else v.get('leasable_area_sq_ft')
        for key,op in [('min_area',lambda a,b:a>=b),('max_area',lambda a,b:a<=b)]:
            if filters.get(key) is not None and (not isinstance(area,(int,float)) or not op(area,float(filters[key]))):return False
        year=v.get('year_built')
        for key,op in [('min_year',lambda a,b:a>=b),('max_year',lambda a,b:a<=b)]:
            if filters.get(key) is not None and (not isinstance(year,(int,float)) or not op(year,float(filters[key]))):return False
        return True

    def catalog(self,filters=None,offset=0,limit=60):
        filters=filters or {};records=self.records();items=[];accounts=set();categories=set();classes=set();qualities=set()
        # Current calculated results are stored with each committed scenario batch.
        for r in records:
            if r.get('reference_only') and not filters.get('include_references'):continue
            snapshot=self.snapshot(r)
            if r.get('calculated'):
                snapshot['values'].update(r['calculated']['values']);snapshot['errors'].update(r['calculated']['errors'])
            if not filters.get('model') or r['model']==filters['model']:
                categories.add(str(snapshot['values'].get('category') or ''))
                classes.add(str(snapshot['values'].get('classification') or ''))
                qualities.add(str(snapshot['values'].get('quality') or ''))
            if self.match(snapshot,filters):items.append(snapshot);accounts.add(snapshot['values'].get('account'))
        items.sort(key=lambda r:(str(r['values'].get('account') or ''),r['model'],r['source_row']))
        with self.connect() as db:revision=db.execute('SELECT revision FROM state').fetchone()[0]
        models=[]
        for key,spec in self.definitions.items():
            labels={}
            cells=self.graph(key)[spec['sheet']]
            for field,column in spec['fields'].items():
                if field.startswith('mix_'):
                    prefix='_'.join(field.split('_')[:2]);quantity=spec['fields'][prefix+'_units']
                    bed=str(cells.get(quantity+'1',{}).get('v') or 'Units').replace('# of ','')
                    suffix={'units':'Units','monthly_rent':'Monthly rent','unit_area_sq_ft':'Unit area (sq ft)'}[field[len(prefix)+1:]]
                    labels[field]=f'{bed} - configuration {int(field.split("_")[1])} - {suffix}'
            models.append({'id':key,'label':LABELS[key],'fields':spec['fields'],'field_labels':labels})
        return {'items':items[offset:offset+limit],'total':len(items),'accounts':len(accounts),'revision':revision,'categories':sorted(categories-{''}),'classifications':sorted(classes-{''}),'qualities':sorted(qualities-{''}),'models':models}

    def detail(self,record_id):
        records=self.records();r=next((r for r in records if r['id']==record_id),None)
        if not r:raise KeyError('Account row not found')
        engine=self.effective_engine(r['model'],records)
        result=self.snapshot(r,engine,details=True)
        result['related']=[self.snapshot(x) for x in records if x['account']==r['account'] and x['id']!=r['id']]
        return result

    def validate(self,field,value):
        if field in TEXT_FIELDS:
            value=str(value).strip()
            if field=='account' and not re.fullmatch(r'R\d+',value.upper()):raise ValueError('Account must start with R followed by digits')
            return value.upper() if field=='account' else value
        if value is None:return None
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):raise ValueError('Enter a finite numeric value')
        if field in {'cap_rate','loaded_cap_rate'} and value<=0:raise ValueError('Cap rate must be greater than zero')
        if (field.endswith('_rate') or field in {'vacancy_rate','other_income_rate'}) and not 0<=value<=1:raise ValueError('Enter rates between 0 and 100%')
        if ('area' in field or field in {'units','rooms'} or field.endswith('_units')) and value<0:raise ValueError('Area and unit counts cannot be negative')
        return value

    def preview(self,payload):
        with self.lock,self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            records=self.records(db);changes=[];skipped=[]
            updates=payload.get('updates')
            if updates is None:updates=[{'field':payload['field'],'operation':payload.get('operation','set'),'value':payload['value']}]
            if not updates or len(updates)>200:raise ValueError('Choose between 1 and 200 inputs to update')
            fields=[u['field'] for u in updates]
            if len(set(fields))!=len(fields):raise ValueError('Each input may only appear once')
            field=fields[0];filters=payload.get('filters',{})
            for u in updates:
                if u.get('operation','set') not in ('set','add','percent'):raise ValueError('Unsupported update operation')
            for r in records:
                if r.get('reference_only'):continue
                snap=self.snapshot(r)
                if not self.match(snap,filters):continue
                spec=self.definitions[r['model']]['fields']
                for u in updates:
                    field=u['field'];operation=u.get('operation','set');amount=u['value'];col=spec.get(field)
                    if not col or not self.editable(r['model'],field):continue
                    if col in r['overrides'] and not payload.get('include_overrides',False):skipped.append(r['id']);continue
                    old=snap['values'].get(field)
                    if operation!='set' and not isinstance(old,(int,float)):raise ValueError(f'{r["account"]}: current value is not numeric')
                    proposed=amount if operation=='set' else old+amount if operation=='add' else old*(1+amount/100)
                    proposed=self.validate(field,proposed)
                    if old!=proposed:changes.append((r,col,proposed))
            field=fields[0]
            if not changes:raise ValueError('No eligible values would change. Check the field, filters and override setting.')
            engines={model:self.effective_engine(model,records,[c for c in changes if c[0]['model']==model]) for model in {r['model'] for r,_,_ in changes}}
            proposed_records=[];preview=[]
            changed_ids={r['id'] for r,_,_ in changes}
            changed_columns={}
            for r,col,_ in changes:changed_columns.setdefault(r['id'],set()).add(col)
            # Recalculate every source row in affected models, including dependent
            # comparison/median rows outside the selection.
            for r in records:
                if r['model'] not in engines:continue
                after=self.snapshot(r,engines[r['model']]);before=self.snapshot(r)
                calculated={'values':after['values'],'errors':after['errors']}
                proposed_records.append({'id':r['id'],'calculated':calculated})
                if r['id'] in changed_ids:
                    output='total_value' if 'total_value' in after['values'] else 'indicated_value'
                    base=(r.get('calculated') or {}).get('values',{}).get(output,before['values'].get(output));new=after['values'].get(output)
                    preview.append({'id':r['id'],'account':r['account'],'name':before['values'].get('name'),'model':r['model'],'category':before['values'].get('category'),'source_row':r['source_row'],'field_changes':[{'field':f,'old':before['values'].get(f),'new':after['values'].get(f)} for f in fields if self.definitions[r['model']]['fields'].get(f) in changed_columns[r['id']]],'old':before['values'].get(field),'new':after['values'].get(field),'before_value':base,'after_value':new,'difference':new-base if isinstance(new,(int,float)) and isinstance(base,(int,float)) else None,'errors':after['errors']})
            token=uuid.uuid4().hex;revision=db.execute('SELECT revision FROM state').fetchone()[0]
            stored={'changes':[{'id':r['id'],'column':col,'value':v} for r,col,v in changes],'calculated':proposed_records,'preview':preview}
            db.execute('INSERT INTO previews VALUES(?,?,?,?)',(token,revision,json.dumps(stored),datetime.now(timezone.utc).isoformat()))
            return {'token':token,'revision':revision,'count':len(preview),'skipped_overrides':len(set(skipped)),'skipped_inputs':len(skipped),'items':preview,'affected_accounts':len({r['account'] for r in preview})}

    def commit(self,token,actor,reason):
        if not reason.strip():raise ValueError('Add a reason for the change')
        with self.lock,self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM previews WHERE id=?',(token,)).fetchone()
            if not row:raise Conflict('Preview is no longer available. Preview again.')
            if row['revision']!=db.execute('SELECT revision FROM state').fetchone()[0]:raise Conflict('The workspace changed. Preview again before applying.')
            if (datetime.now(timezone.utc)-datetime.fromisoformat(row['created_at'])).total_seconds()>1800:raise Conflict('Preview expired. Preview again.')
            stored=json.loads(row['payload']);by_id={};history=[]
            for change in stored['changes']:by_id.setdefault(change['id'],[]).append(change)
            for update in stored['calculated']:
                r=db.execute('SELECT * FROM records WHERE id=?',(update['id'],)).fetchone();meta=json.loads(r['metadata']);overrides=json.loads(r['overrides'])
                before={'metadata':json.loads(r['metadata']),'overrides':overrides.copy()}
                for change in by_id.get(update['id'],[]):overrides[change['column']]=change['value']
                meta['calculated']=update['calculated']
                db.execute('UPDATE records SET metadata=?,overrides=? WHERE id=?',(json.dumps(meta),json.dumps(overrides),update['id']))
                history.append({'id':update['id'],'before':before,'after':{'metadata':meta,'overrides':overrides}})
            batch_id=uuid.uuid4().hex
            db.execute('INSERT INTO batches(id,actor,reason,created_at,changes) VALUES(?,?,?,?,?)',(batch_id,actor,reason,datetime.now(timezone.utc).isoformat(),json.dumps(history)))
            db.execute('UPDATE state SET revision=revision+1');db.execute('DELETE FROM previews')
            return {'batch_id':batch_id,'updated':len(by_id)}

    def history(self):
        with self.connect() as db:return [dict(r) for r in db.execute('SELECT id,actor,reason,created_at,undone FROM batches ORDER BY created_at DESC LIMIT 30')]

    def undo(self,batch_id,actor):
        with self.lock,self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            batch=db.execute('SELECT * FROM batches WHERE id=?',(batch_id,)).fetchone()
            if not batch or batch['undone']:raise Conflict('Batch is unavailable or already undone')
            latest=db.execute('SELECT id FROM batches WHERE undone=0 ORDER BY created_at DESC LIMIT 1').fetchone()
            if not latest or latest['id']!=batch_id:raise Conflict('Undo the most recent batch first to preserve subsequent work')
            for c in json.loads(batch['changes']):
                if c['before'] is None:
                    db.execute('DELETE FROM records WHERE id=?',(c['id'],))
                    continue
                db.execute('UPDATE records SET metadata=?,overrides=? WHERE id=?',(json.dumps(c['before']['metadata']),json.dumps(c['before']['overrides']),c['id']))
            db.execute('UPDATE batches SET undone=1 WHERE id=?',(batch_id,));db.execute('UPDATE state SET revision=revision+1');db.execute('DELETE FROM previews')
            return {'undone':batch_id,'actor':actor}

    def create(self,template_id,fields,actor):
        with self.lock,self.connect() as db:
            db.execute('BEGIN IMMEDIATE');records=self.records(db);template=next((r for r in records if r['id']==template_id),None)
            if not template:raise ValueError('Choose an existing property as the model template')
            if template.get('reference_only'):raise ValueError('Choose a populated property row as the model template')
            account=self.validate('account',fields.get('account',''))
            if any(r['account']==account and r['model']==template['model'] for r in records):raise Conflict('This account already exists in that model')
            spec=self.definitions[template['model']]['fields'];record_id=uuid.uuid4().hex;oldrow=template['source_row']
            row=max([r['source_row'] for r in records if r['model']==template['model']]+[999999])+1
            if row>1048576:raise ValueError('Model row capacity reached')
            source=self.graph(template['model'])[template['source_sheet']];extra={};overrides=dict(template['overrides'])
            if template['model']!='cost':
                indicated=spec.get('indicated_value') or spec.get('total_value')
                if not (template['extra_cells'] or source).get(f'{indicated}{oldrow}',{}).get('f'):
                    raise ValueError('This row has no income calculation to extend. Choose a property with a complete income model as the template.')
            engine=self.effective_engine(template['model'],records)
            for addr,c in (template['extra_cells'] or source).items():
                if re.search(r'\d+$',addr)[0]!=str(oldrow):continue
                col=addr.rstrip('0123456789');target=f'{col}{row}';new=dict(c)
                if c.get('f'):
                    try:
                        tokens=Tokenizer('='+c['f']).items
                        for token in tokens:
                            if token.type=='OPERAND' and token.subtype=='RANGE':
                                match=re.fullmatch(r'(\$?[A-Z]+)(\d+)',token.value)
                                if match and int(match[2])==oldrow:token.value=match[1]+str(row)
                        new['f']=''.join(t.value for t in tokens)
                        new['force_recalculate']=True
                    except Exception:raise ValueError('This template has a formula that cannot be extended')
                if col in template['overrides']:new['v']=template['overrides'][col];new.pop('f',None)
                extra[target]=new
            for field,value in fields.items():
                if field not in spec or field in OUTPUTS or field=='type':continue
                overrides[spec[field]]=self.validate(field,value)
            meta={k:v for k,v in template.items() if k not in ('overrides','extra_cells','calculated')}
            meta.update({'id':record_id,'account':account,'source_row':row,'created_by':actor,'created_at':datetime.now(timezone.utc).isoformat(),'template_id':template_id,'new_property':True})
            meta['reference_only']=False
            db.execute('INSERT INTO records VALUES(?,?,?,?)',(record_id,json.dumps(meta),json.dumps(overrides),json.dumps(extra)))
            db.execute('INSERT INTO batches(id,actor,reason,created_at,changes) VALUES(?,?,?,?,?)',(uuid.uuid4().hex,actor,f'Add {account} to {LABELS[meta["model"]]}',datetime.now(timezone.utc).isoformat(),json.dumps([{'id':record_id,'before':None,'after':{'metadata':meta,'overrides':overrides}}])))
            db.execute('UPDATE state SET revision=revision+1');db.execute('DELETE FROM previews')
        return self.detail(record_id)
