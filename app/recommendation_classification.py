"""User assertions linking fixed recommendations; no orders or ledger changes."""
from contextlib import closing,contextmanager,ExitStack
from datetime import date,datetime
import hashlib
import json
from pathlib import Path
import re
import secrets
import sqlite3
from threading import Lock
from time import monotonic
from app.actual_trade_records import normalize,input_lock
from app.morning_quote_router import _safe_path,_plan_identity
from app.morning_virtual import MorningVirtual,record_path,aware
from app.storage import Database
from app.trading_clock import KST

COMMANDS={'/추천매수연결','/추천미구매','/추천분류철회','/추천분류확인'}
NOTICE='사용자의 분류이며 증권사 검증·미구매 증명이 아니에요. 기존 실제/가상 손익과 주문은 바꾸지 않아요.'
USAGE='/추천매수연결 YYYY-MM-DD 종목코드 체결번호\n/추천미구매 YYYY-MM-DD 종목코드\n/추천분류철회 YYYY-MM-DD 종목코드\n확정 후 /추천대조 에서 확인해요.'
LABELS={'ACTUAL_LINK':'실제입력 연결','NOT_BOUGHT':'미구매 사용자확인','UNKNOWN':'미확인'}


def packed(value):return json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False,separators=(',',':'))
def digest(value):return hashlib.sha256(packed(value).encode()).hexdigest()


def research_path(root):
    path=Path(root)/'research.db';_safe_path(root,path)
    if not path.is_file():raise ValueError()
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='daily_bars'").fetchone():raise ValueError()
    return path


def source(root,day,code,fill_id,now):
    if not aware(now) or date.fromisoformat(day).isoformat()!=day or day>now.astimezone(KST).date().isoformat():raise ValueError()
    path=record_path(root,'REAL_QUOTES',day);_safe_path(root,path)
    if not path.is_file():raise ValueError()
    with Database(path,read_only=True).connect() as db:
        db.execute('BEGIN');plan=MorningVirtual.plan_from_connection(db);_plan_identity(plan,'REAL_QUOTES',day)
    target=next((t for t in plan['targets'] if t['code']==code),None)
    if target is None:raise ValueError()
    actual=[];selected=None
    path=Path(root)/'actual_fill_records.db';_safe_path(root,path)
    if path.is_file():
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
            db.execute('BEGIN')
            if db.execute('SELECT kind FROM actual_meta').fetchall()!=[('REAL_USER_REPORTED',)]:raise ValueError()
            rows=db.execute('SELECT id,day,payload,evidence_hash FROM actual_fills WHERE day=? ORDER BY id',(day,)).fetchall()
        for row in rows:
            if row[1]!=day or not isinstance(row[3],str) or not re.fullmatch('[a-f0-9]{64}',row[3]):raise ValueError()
            raw=json.loads(row[2]);values=dict(raw)
            for field in ('quantity','price_won','realized_pnl_won'):values[field]='' if raw[field] is None else str(raw[field])
            fill=normalize(values,now)
            if fill!=raw or fill['fill_id']!=row[0] or fill['filled_at'][:10]!=day:raise ValueError()
            if fill['code']==code and fill['side']=='BUY':
                actual.append(dict(payload=raw,evidence_hash=row[3]))
                if row[0]==fill_id:selected=fill
    if fill_id is not None and selected is None:raise ValueError()

    return dict(plan=plan,actual=actual)


@contextmanager
def locked_sources(root,day):
    """Reserve source writers only; never execute source content writes."""
    with ExitStack() as stack:
        stack.enter_context(input_lock(Path(root)/'actual_fill_records.db'))
        for path in (record_path(root,'REAL_QUOTES',day),Path(root)/'actual_fill_records.db'):
            _safe_path(root,path)
            if not path.is_file():
                if path.name=='actual_fill_records.db':continue
                raise ValueError()
            db=sqlite3.connect(path.resolve().as_uri()+'?mode=rw',uri=True,timeout=.1)
            stack.callback(db.close);stack.callback(db.rollback)
            db.execute('BEGIN IMMEDIATE')
        yield


def permitted(mode,active):
    return (mode() if callable(mode) else mode)=='REAL_QUOTES' and (active() if callable(active) else active) is True


def valid_owner(value):return isinstance(value,str) and re.fullmatch('[A-Za-z0-9_-]{1,128}',value) is not None


def latest(root,day):
    path=Path(root)/'research.db';_safe_path(root,path)
    if not path.is_file():return {}
    research_path(root)
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.execute('BEGIN')
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='recommendation_classifications'").fetchone():return {}
        rows=db.execute('SELECT id,payload,hash FROM recommendation_classifications ORDER BY id').fetchall()
    result={}
    for identifier,payload,hash_value in rows:
        value=json.loads(payload)
        if not isinstance(value,dict) or set(value)!={'mode','day','code','state','fill_id','source_hash','scope','session','confirmed_at'}:raise ValueError()
        confirmed=datetime.fromisoformat(value['confirmed_at'])
        if not aware(confirmed) or confirmed.astimezone(KST).date().isoformat()<value['day'] or not valid_owner(value['scope']) or not valid_owner(value['session']):raise ValueError()
        if (digest(value)!=hash_value or value.get('mode')!='REAL_QUOTES' or value.get('state') not in LABELS
                or not isinstance(value.get('code'),str) or not re.fullmatch('[A-Za-z0-9]{6}',value['code'])
                or not isinstance(value.get('source_hash'),str) or not re.fullmatch('[a-f0-9]{64}',value['source_hash'])
                or (value['state']=='ACTUAL_LINK' and (not isinstance(value.get('fill_id'),str) or not re.fullmatch('[A-Za-z0-9_-]{1,80}',value['fill_id'])))
                or (value['state']!='ACTUAL_LINK' and value.get('fill_id') is not None)
                or date.fromisoformat(value['day']).isoformat()!=value['day']):raise ValueError()
        if value['day']==day:result[value['code']]=dict(value,id=identifier)
    return result


def classifications(root,day,now):
    result={}
    try:rows=latest(root,day)
    except Exception:raise ValueError('추천 분류 이력을 확인하지 못했어요. 분류를 추측하지 않았어요.') from None
    for code,row in rows.items():
        try:
            if datetime.fromisoformat(row['confirmed_at'])>now:raise ValueError()
            current=source(root,day,code,row['fill_id'],now)
            if digest(current)!=row['source_hash']:raise ValueError()
            result[code]=LABELS[row['state']]
        except Exception:result[code]='미확인 · 원본 변경/확인 실패'
    return result


class Classification:
    def __init__(self,root,*,clock=monotonic):self.root=Path(root);self.clock=clock;self.lock=Lock();self.pending={}

    def handle(self,text,*,mode,scope,session,active,now):
        with self.lock:
            try:return self._handle(text,mode,scope,session,active,now)
            except Exception:return '추천 목록·체결 입력·분류 기록을 확인하지 못했어요. 변경하지 않았어요.'

    def _handle(self,text,mode,scope,session,active,now):
        if not permitted(mode,active):
            self.pending.pop((scope,session),None)
            return '실제 시세 보기 모드의 현재 개인 수신에서만 분류를 바꿀 수 있어요.'
        if not aware(now) or not valid_owner(scope) or not valid_owner(session):raise ValueError()
        research_path(self.root)
        parts=text.split();owner=(scope,session)
        if not parts:return USAGE
        if parts[0]=='/추천분류확인':
            if len(parts)!=2:return USAGE
            request=self.pending.pop(owner,None)
            if request is None or not secrets.compare_digest(parts[1],request['token']) or not 0<=self.clock()-request['at']<120:
                return '확인이 만료됐거나 이 수신의 요청이 아니에요. 처음부터 다시 요청하세요.'
            value=request['value'];day=value['day'];code=value['code']
            current=source(self.root,day,code,value['fill_id'],now)
            if digest(current)!=value['source_hash']:return '추천/실제 입력이 바뀌었어요. 저장하지 않았으니 다시 요청하세요.'
            path=research_path(self.root)
            with locked_sources(self.root,day),closing(sqlite3.connect(path.resolve().as_uri()+'?mode=rw',uri=True)) as db,db:
                db.execute('BEGIN IMMEDIATE')
                if not permitted(mode,active):raise ValueError()
                # Recheck history after acquiring the writer lock.
                if latest(self.root,day).get(code)!=request['base']:raise ValueError()
                if digest(source(self.root,day,code,value['fill_id'],now))!=value['source_hash']:raise ValueError()
                if not permitted(mode,active):raise ValueError()
                db.execute('CREATE TABLE IF NOT EXISTS recommendation_classifications(id INTEGER PRIMARY KEY,payload TEXT NOT NULL,hash TEXT NOT NULL)')
                value=dict(value,scope=scope,session=session,confirmed_at=now.isoformat())
                db.execute('INSERT INTO recommendation_classifications(payload,hash) VALUES(?,?)',(packed(value),digest(value)))
                if digest(source(self.root,day,code,value['fill_id'],now))!=value['source_hash']:raise ValueError()
                if not permitted(mode,active):raise ValueError()
            return LABELS[value['state']]+' 저장했어요. /추천대조 로 확인하세요.\n'+NOTICE
        if parts[0] not in COMMANDS or len(parts)!=(4 if parts[0]=='/추천매수연결' else 3):return USAGE
        day,code=parts[1:3]
        if not re.fullmatch('[A-Za-z0-9]{6}',code):return USAGE
        fill_id=parts[3] if len(parts)==4 else None
        state={'/추천매수연결':'ACTUAL_LINK','/추천미구매':'NOT_BOUGHT','/추천분류철회':'UNKNOWN'}[parts[0]]
        src=source(self.root,day,code,fill_id,now);base=latest(self.root,day).get(code)
        if not permitted(mode,active):raise ValueError()
        value=dict(mode='REAL_QUOTES',day=day,code=code,state=state,fill_id=fill_id,source_hash=digest(src))
        token=secrets.token_hex(8)
        # One preview per receiver. Old receiver previews cannot be confirmed by a new one.
        self.pending={key:val for key,val in self.pending.items() if 0<=self.clock()-val['at']<120 and key[0]!=scope}
        self.pending[owner]=dict(token=token,at=self.clock(),value=value,base=base)
        return f'{day} · {code} → {LABELS[state]}\n2분 안에 /추천분류확인 {token}\n'+NOTICE
