"""Bounded interpreter for model formulas. Never executes workbook code.

Unchanged cells retain Excel's saved result. Changed dependencies are evaluated
from formula text; missing external references stay unavailable rather than 0.
"""
from __future__ import annotations

import math
import re
import statistics
from functools import lru_cache

from openpyxl.formula import Tokenizer
from openpyxl.utils.cell import range_boundaries, get_column_letter


class FormulaError(ValueError):
    pass


class UnsupportedFormula(FormulaError):
    pass


def number(value):
    if value in (None, ""):
        return 0.0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)) and math.isfinite(value):
        return value
    raise FormulaError(f"Non-numeric input: {str(value)[:60]}")


@lru_cache(maxsize=30000)
def parse(formula):
    tokens = [t for t in Tokenizer('='+formula.lstrip('=')).items if t.type != 'WHITE-SPACE']
    index = 0
    precedence = {'=':1,'<>':1,'<':1,'>':1,'<=':1,'>=':1,'&':2,'+':3,'-':3,'*':4,'/':4,'^':5}

    def expr(minimum=0):
        nonlocal index
        if index >= len(tokens):
            return ('literal', None)
        t=tokens[index]; index+=1
        if t.type == 'OPERATOR-PREFIX':
            left=('unary',t.value,expr(6))
        elif t.type == 'PAREN' and t.subtype=='OPEN':
            left=expr(); index+=1
        elif t.type=='FUNC' and t.subtype=='OPEN':
            args=[]
            while index<len(tokens) and not (tokens[index].type=='FUNC' and tokens[index].subtype=='CLOSE'):
                if tokens[index].type=='SEP':
                    args.append(('literal',None));index+=1;continue
                args.append(expr())
                if index<len(tokens) and tokens[index].type=='SEP':index+=1
            if index>=len(tokens):raise FormulaError('Unclosed formula function')
            index+=1;left=('call',t.value[:-1].upper().removeprefix('_XLFN.'),tuple(args))
        elif t.type=='OPERAND':
            if t.subtype=='RANGE':left=('ref',t.value)
            elif t.subtype=='ERROR':left=('error',t.value)
            elif t.subtype=='TEXT':left=('literal',t.value[1:-1].replace('""','"'))
            elif t.subtype=='LOGICAL':left=('literal',t.value=='TRUE')
            else:left=('literal',float(t.value))
        else:raise UnsupportedFormula(f'Unsupported formula token {t.value}')
        while index<len(tokens):
            t=tokens[index]
            if t.type=='OPERATOR-POSTFIX' and t.value=='%':
                index+=1;left=('binary','/',left,('literal',100));continue
            p=precedence.get(t.value,-1) if t.type=='OPERATOR-INFIX' else -1
            if p<minimum:break
            index+=1;right=expr(p+1);left=('binary',t.value,left,right)
        return left
    result=expr()
    if index!=len(tokens):raise UnsupportedFormula('Unsupported formula syntax')
    return result


def references(node):
    if node[0]=='ref':yield node[1]
    elif node[0]=='call':
        for arg in node[2]:yield from references(arg)
    elif node[0]=='binary':
        yield from references(node[2]);yield from references(node[3])
    elif node[0]=='unary':yield from references(node[2])


class Engine:
    def __init__(self, sheets, overrides=None):
        self.sheets=sheets
        self.overrides=overrides or {}
        self.cache={}
        self.dirty_cache={}
        self.visiting=set()

    def addresses(self, reference, sheet):
        reference=reference.replace('$','')
        if '!' in reference:
            sheet,reference=reference.rsplit('!',1)
            sheet=sheet.strip("'").replace("''", "'")
        if '[' in sheet or sheet not in self.sheets:
            raise UnsupportedFormula('Missing source workbook or worksheet')
        if not re.fullmatch(r'[A-Z]+[0-9]+(?::[A-Z]+[0-9]+)?',reference,re.I):
            raise UnsupportedFormula(f'Unsupported or unbounded reference: {reference[:50]}')
        a,b,c,d=range_boundaries(reference)
        if (c-a+1)*(d-b+1)>100000:
            raise UnsupportedFormula('Reference exceeds calculation limit')
        return sheet, [[f'{get_column_letter(col)}{row}' for col in range(a,c+1)] for row in range(b,d+1)]

    def dirty(self,sheet,address,trail=None):
        key=(sheet,address)
        if key in self.overrides:return True
        if key in self.dirty_cache:return self.dirty_cache[key]
        trail=set() if trail is None else trail
        if key in trail:return False
        cell=self.sheets.get(sheet,{}).get(address,{})
        if cell.get('force_recalculate') and cell.get('f'):return True
        result=False
        if cell.get('f'):
            try:
                for ref in references(parse(cell['f'])):
                    target,grid=self.addresses(ref,sheet)
                    if any(self.dirty(target,a,trail|{key}) for row in grid for a in row):
                        result=True;break
            except (FormulaError,ValueError,RecursionError):
                # An unparseable formula cannot silently retain a stale result.
                result=bool(self.overrides)
        self.dirty_cache[key]=result
        return result

    def cell(self,sheet,address):
        key=(sheet,address)
        if key in self.overrides:return self.overrides[key]
        if key in self.cache:return self.cache[key]
        data=self.sheets.get(sheet,{}).get(address,{})
        if data.get('error') and not (data.get('f') and self.dirty(sheet,address)):raise FormulaError(f"Source {sheet}!{address}: {data['v']}")
        if not data.get('f') or not self.dirty(sheet,address):return data.get('v')
        if key in self.visiting:raise FormulaError('Circular model reference')
        self.visiting.add(key)
        try:
            result=self.evaluate(parse(data['f']),sheet)
            if isinstance(result,float) and not math.isfinite(result):raise FormulaError('Non-finite result')
            self.cache[key]=result
            return result
        except (ZeroDivisionError,OverflowError,TypeError,RecursionError) as exc:
            raise FormulaError('Model calculation unavailable: '+type(exc).__name__) from exc
        finally:self.visiting.discard(key)

    def evaluate(self,node,sheet):
        kind=node[0]
        if kind=='literal':return node[1]
        if kind=='error':raise FormulaError(node[1])
        if kind=='ref':
            target,grid=self.addresses(node[1],sheet)
            if len(grid)==len(grid[0])==1:return self.cell(target,grid[0][0])
            return CellRange(self,target,grid)
        if kind=='unary':return number(self.evaluate(node[2],sheet))*(-1 if node[1]=='-' else 1)
        if kind=='binary':
            a=self.evaluate(node[2],sheet);b=self.evaluate(node[3],sheet);op=node[1]
            if op=='&':return str(a or '')+str(b or '')
            if op in ('=','<>','<','>','<=','>='):
                if isinstance(a,str) and isinstance(b,str):a,b=a.casefold(),b.casefold()
                if a is None:a=0 if isinstance(b,(int,float)) else ''
                if b is None:b=0 if isinstance(a,(int,float)) else ''
                return {'=':lambda:a==b,'<>':lambda:a!=b,'<':lambda:a<b,'>':lambda:a>b,'<=':lambda:a<=b,'>=':lambda:a>=b}[op]()
            a,b=number(a),number(b)
            return {'+':lambda:a+b,'-':lambda:a-b,'*':lambda:a*b,'/':lambda:a/b,'^':lambda:a**b}[op]()
        fn,args=node[1:]
        if fn=='IF':
            return self.evaluate(args[1] if self.evaluate(args[0],sheet) else (args[2] if len(args)>2 else ('literal',False)),sheet)
        if fn in ('IFERROR','IFNA'):
            try:return self.evaluate(args[0],sheet)
            except UnsupportedFormula:raise
            except (FormulaError,ZeroDivisionError,OverflowError):
                if fn=='IFNA':raise
                return self.evaluate(args[1],sheet)
        values=[self.evaluate(a,sheet) for a in args]
        def flatten(v):
            if isinstance(v,(list,CellRange)):
                for x in v:yield from flatten(x)
            else:yield v
        nums=[v for v in flatten(values) if isinstance(v,(int,float)) and not isinstance(v,bool)] if fn in ('SUM','AVERAGE','MEDIAN') else []
        if fn=='SUM':return sum(nums)
        if fn=='AVERAGE':return statistics.mean(nums) if nums else (_ for _ in ()).throw(FormulaError('No numeric values'))
        if fn=='MEDIAN':return statistics.median(nums) if nums else (_ for _ in ()).throw(FormulaError('No numeric values'))
        if fn=='ABS':return abs(number(values[0]))
        if fn in ('ROUND','ROUNDDOWN'):
            n=number(values[0]);places=int(number(values[1]));scale=10**places
            return (math.floor(abs(n)*scale+0.5) if fn=='ROUND' else math.floor(abs(n)*scale))*math.copysign(1,n)/scale
        if fn=='VLOOKUP':
            key,table,col=values[:3];approx=bool(values[3]) if len(values)>3 else True
            candidate=None
            if not isinstance(table,CellRange):raise FormulaError('Lookup requires a cell range')
            for index in range(len(table.grid)):
                first=table.get(index,0)
                if str(first).casefold()==str(key).casefold():return table.get(index,int(col)-1)
                if approx and first is not None and first<=key:candidate=index
            if candidate is not None:return table.get(candidate,int(col)-1)
            raise FormulaError('Lookup key not found')
        if fn=='MATCH':
            key=values[0];flat=list(flatten(values[1]));mode=values[2] if len(values)>2 else 1
            if mode!=0:raise FormulaError('Approximate MATCH needs validation')
            for i,v in enumerate(flat):
                if str(v).casefold()==str(key).casefold():return i+1
            raise FormulaError('Match key not found')
        if fn=='INDEX':
            table=values[0];row=int(values[1])-1;col=int(values[2])-1 if len(values)>2 else 0
            return table.get(row,col) if isinstance(table,CellRange) else table[row][col]
        raise UnsupportedFormula(f'Function {fn} needs validation')


class CellRange:
    """Resolve only the lookup cells Excel needs, not unrelated error cells."""
    def __init__(self,engine,sheet,grid):self.engine,self.sheet,self.grid=engine,sheet,grid
    def get(self,row,col):
        if row<0 or col<0:raise FormulaError('Invalid lookup index')
        try:return self.engine.cell(self.sheet,self.grid[row][col])
        except IndexError as exc:raise FormulaError('Lookup index is out of range') from exc
    def __iter__(self):
        for row in self.grid:yield [self.engine.cell(self.sheet,a) for a in row]
