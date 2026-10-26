"""REAL quote-only 0B capture. Local first-seen candidates, never first trades."""
from contextlib import closing
from datetime import datetime
import hashlib
import json
import logging
from pathlib import Path
import re
import sqlite3
import time
import uuid

from app.morning_open_evidence_status import session_bounds
from app.morning_quote_router import _safe_path,_plan_identity
from app.morning_virtual import MorningVirtual,record_path,aware
from app.morning_venue_policy import stored_policy,KRX_OPEN
from app.storage import Database
from app.runtime_lock import ReceiverLock
from app.trading_clock import KST

URL='wss://api.kiwoom.com:10000/api/dostk/websocket'
AUTH_URL='https://api.kiwoom.com'
MAX_TARGETS=100
FIELDS=set('20 10 11 12 27 28 15 13 14 16 17 18 25 26 29 30 31 32 228 311 290 691 567 568 851 1890 1891 1892 1030 1031 1032 1071 1072 1313 1315 1316 1314 1497 1498 620 9081'.split())
DISCLAIMER='처음 받은 후보이며 공식 KRX 첫 거래·시초가 인증이 아니에요. 거래소 순번·누락 복구·정렬·서명·수집 완전성은 미확인입니다.'


class CaptureFailure(ValueError):
    def __init__(self,reason):self.reason=reason


def _unique_object(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise CaptureFailure('DUPLICATE_JSON_KEY')
        result[key]=value
    return result


def connector():
    from websockets.sync.client import connect
    logger=logging.Logger('krx_quote_capture_private',level=logging.CRITICAL+1)
    logger.addHandler(logging.NullHandler());logger.propagate=False
    return connect(URL,proxy=None,compression=None,open_timeout=5,close_timeout=1,
                   ping_interval=20,ping_timeout=20,max_size=65536,max_queue=16,logger=logger)


def targets(root,now):
    if not aware(now):raise ValueError('확인 시각을 확인해 주세요.')
    day=now.astimezone(KST).date().isoformat();_,end=session_bounds(day)
    if now>end:raise ValueError('오늘 정규장이 끝났어요. 다음 거래일 목록을 고정한 뒤 시작해 주세요.')
    path=record_path(root,'REAL_QUOTES',day);_safe_path(root,path)
    if not path.is_file():raise ValueError('오늘 REAL 시세 모드의 아침 가상 목록을 먼저 고정해 주세요.')
    with Database(path,read_only=True).connect() as db:
        db.execute('BEGIN');plan=MorningVirtual.plan_from_connection(db)
        _plan_identity(plan,'REAL_QUOTES',day)
    if stored_policy(plan)!=KRX_OPEN or not 1<=len(plan['targets'])<=MAX_TARGETS:
        raise ValueError('KRX 새 목록 1~100종목만 지원해요. 초과 대상을 잘라서 수집하지 않아요.')
    return plan


def _record_path(root):
    path=Path(root)/'research.db';_safe_path(root,path)
    return path


def _connect(path,readonly=False):
    return sqlite3.connect(path.resolve().as_uri()+('?mode=ro' if readonly else '?mode=rw'),uri=True,timeout=2)


def summary(root):
    path=_record_path(root)
    if not path.is_file():return '시세 수집 기록이 없어요. '+DISCLAIMER
    try:
        with closing(_connect(path,True)) as db:
            db.execute('BEGIN')
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='quote_capture_sessions'").fetchone():return '시세 수집 기록이 없어요. '+DISCLAIMER
            row=db.execute('SELECT id,day FROM quote_capture_sessions ORDER BY rowid DESC LIMIT 1').fetchone()
            if row is None:return '시세 수집 기록이 없어요. '+DISCLAIMER
            last=db.execute('SELECT stage FROM quote_capture_events WHERE session=? ORDER BY seq DESC LIMIT 1',(row[0],)).fetchone()
            frames,candidates=db.execute('SELECT COUNT(*),COALESCE(SUM(candidate_count),0) FROM quote_capture_frames WHERE session=?',(row[0],)).fetchone()
        labels={'OPENING':'연결 준비','LOGIN_ACK':'로그인 응답 확인','REG_SENT':'등록 요청 전송','REG_ACK':'등록 응답 확인',
                'FRAME':'마지막 시세 저장','PRE_REGISTER_FRAME':'등록 응답 전 시세','PING':'연결 신호','STOPPED':'중지됨','FAILED':'중단·자료 확인 필요'}
        return '현재 연결 여부는 PC 수집 상태로 확인하세요. 종료 기록이 없으면 연결 지속은 미확인입니다.\n'+f'[시세 수집 · REAL 전용] {row[1]}\n{labels.get(last[0] if last else "", "상태 미확인")} · 수신 {frames}묶음 · 처음 받은 후보 {candidates}개\n등록 응답은 로컬 확인 시각입니다. 거래소구독 효력·9081 값의 거래소 해석은 미확인입니다.\n'+DISCLAIMER
    except Exception:return '시세 수집 기록을 읽지 못했어요. 상태는 미확인입니다. '+DISCLAIMER


class Capture:
    """One connection/session. Inject auth, socket, clocks for offline tests."""
    def __init__(self,root,auth_factory,*,socket_factory=connector,clock=None,monotonic=time.monotonic,max_frames=100000):
        self.root=Path(root);self.auth_factory=auth_factory;self.socket_factory=socket_factory
        self.clock=clock or (lambda:datetime.now(KST));self.monotonic=monotonic;self.max_frames=max_frames
        self.session=None;self.socket=None;self.auth=None;self.seq=0;self.state='NEW';self.seen=set();self.last={};self.candidates=set();self.frames=0

    def event(self,stage,**details):
        self.seq+=1
        with closing(_connect(self.path)) as db:
            db.execute('INSERT INTO quote_capture_events VALUES(?,?,?,?,?,?)',(self.session,self.seq,stage,self.clock().isoformat(),self.monotonic()-self.started,json.dumps(details,sort_keys=True)));db.commit()

    def run(self,*,active=lambda:True):
        if self.state!='NEW':return 'FAILED'
        lock=None
        try:
            if not active():return 'CANCELED'
            plan=targets(self.root,self.clock());self.day=plan['day'];self.codes={r['code'] for r in plan['targets']}
            self.path=_record_path(self.root)
            if not self.path.is_file():raise ValueError()
            lock=ReceiverLock(self.root/'quote_capture.lock');lock.__enter__()
            if not active():return 'CANCELED'
            self.session=uuid.uuid4().hex;self.started=self.monotonic()
            with closing(_connect(self.path)) as db:
                if not db.execute("SELECT 1 FROM sqlite_master WHERE name='daily_bars'").fetchone():raise ValueError()
                db.executescript('''CREATE TABLE IF NOT EXISTS quote_capture_sessions(id TEXT PRIMARY KEY,day TEXT,mode TEXT,plan_hash TEXT,targets_json TEXT,started_at TEXT);
                    CREATE TABLE IF NOT EXISTS quote_capture_events(session TEXT,seq INTEGER,stage TEXT,at TEXT,elapsed REAL,details TEXT,PRIMARY KEY(session,seq));
                    CREATE TABLE IF NOT EXISTS quote_capture_frames(session TEXT,seq INTEGER,raw TEXT,sha256 TEXT,candidate_count INTEGER,details TEXT,PRIMARY KEY(session,seq));''')
                payload=json.dumps(plan,sort_keys=True,ensure_ascii=False,allow_nan=False)
                db.execute('INSERT INTO quote_capture_sessions VALUES(?,?,?,?,?,?)',(self.session,self.day,'REAL_QUOTES',hashlib.sha256(payload.encode()).hexdigest(),json.dumps(sorted(self.codes)),self.clock().isoformat()));db.commit()
            self.event('OPENING',source='KIWOOM_0B_REAL',endpoint=URL,contract='v1',requested_venue='KRX',provider_exchange_interpretation='UNKNOWN',coverage='UNVERIFIED')
            if not active():return self.stop('OWNER_CHANGED')
            self.auth=self.auth_factory()
            if getattr(self.auth,'url',None)!=AUTH_URL:raise ValueError()
            if not active():return self.stop('OWNER_CHANGED')
            self.auth.login()
            token=self.auth.token
            if not isinstance(token,str) or not token or len(token)>8192 or any(c.isspace() for c in token):raise ValueError()
            if not active():return self.stop('OWNER_CHANGED')
            self.socket=self.socket_factory();self.socket.send(json.dumps(dict(trnm='LOGIN',token=token)))
            self.state='LOGIN_WAIT';self.handshake=self.monotonic()
            while active():
                now=self.clock()
                if not aware(now) or now.astimezone(KST).date().isoformat()!=self.day:raise CaptureFailure('DATE_CHANGED')
                if now>session_bounds(self.day)[1]:return self.stop('SESSION_ENDED')
                if self.state!='RECEIVING' and self.monotonic()-self.handshake>10:raise CaptureFailure('HANDSHAKE_TIMEOUT')
                try:raw=self.socket.recv(timeout=1)
                except TimeoutError:continue
                except Exception:raise CaptureFailure('DISCONNECTED') from None
                if not active():return self.stop('OWNER_CHANGED')
                received=self.clock()
                if not aware(received) or received.astimezone(KST).date().isoformat()!=self.day:raise CaptureFailure('DATE_CHANGED')
                if received>session_bounds(self.day)[1]:return self.stop('SESSION_ENDED')
                self.receive(raw)
            return self.stop('REQUESTED')
        except Exception as error:
            reason=error.reason if isinstance(error,CaptureFailure) else 'STORAGE_FAILURE' if isinstance(error,sqlite3.Error) else 'PROTOCOL_OR_AUTH_FAILURE'
            self.state='FAILED'
            if self.session is not None:
                try:self.event('FAILED',reason=reason,coverage='UNVERIFIED')
                except Exception:pass
            return 'FAILED'
        finally:
            if self.socket is not None:
                try:self.socket.close()
                except Exception:pass
            if self.auth is not None:
                self.auth.token=None
                try:self.auth.session.close()
                except Exception:pass
            if lock is not None:lock.__exit__(None,None,None)

    def stop(self,reason):
        self.event('STOPPED',reason=reason,coverage='UNVERIFIED');self.state='STOPPED';return self.state

    def receive(self,raw):
        now=self.clock()
        if not aware(now) or now.astimezone(KST).date().isoformat()!=self.day:raise CaptureFailure('DATE_CHANGED')
        if self.state!='RECEIVING' and self.monotonic()-self.handshake>10:raise CaptureFailure('HANDSHAKE_TIMEOUT')
        if not isinstance(raw,str) or len(raw.encode('utf-8'))>65536:raise CaptureFailure('OVERSIZE')
        frame=json.loads(raw,object_pairs_hook=_unique_object)
        if not isinstance(frame,dict):raise ValueError()
        kind=frame.get('trnm')
        if kind=='PING':
            if set(frame)!={'trnm'}:raise ValueError()
            self.socket.send(raw);self.event('PING');return
        if kind in ('LOGIN','REG'):
            if set(frame)-{'trnm','return_code','return_msg','grp_no'} or type(frame.get('return_code')) is not int or frame['return_code']!=0:raise ValueError()
            if kind=='LOGIN':
                if self.state!='LOGIN_WAIT':raise ValueError()
                self.event('LOGIN_ACK');self.socket.send(json.dumps(dict(trnm='REG',grp_no='1',refresh='0',data=[dict(item=sorted(self.codes),type=['0B'])])))
                self.event('REG_SENT');self.state='REGISTER_WAIT'
            else:
                if self.state!='REGISTER_WAIT' or frame.get('grp_no','1')!='1':raise ValueError()
                self.event('REG_ACK',effect_time='UNKNOWN');self.state='RECEIVING'
            return
        if kind!='REAL' or self.state not in ('REGISTER_WAIT','RECEIVING') or set(frame)!={'trnm','data'}:raise ValueError()
        rows=frame['data']
        if not isinstance(rows,list) or not 1<=len(rows)<=100:raise ValueError()
        if self.frames>=self.max_frames:raise CaptureFailure('BUFFER_LIMIT')
        received=self.clock();now=self.monotonic();parsed=[];previous=dict(self.last)
        digest=hashlib.sha256(raw.encode()).hexdigest();duplicate=digest in self.seen
        for row in rows:
            if (not isinstance(row,dict) or set(row)-{'type','item','values','name'} or row.get('type')!='0B'
                    or row.get('item') not in self.codes or row.get('name','주식체결')!='주식체결'):raise ValueError()
            values=row.get('values')
            if not isinstance(values,dict) or set(values)-FIELDS or not {'20','10','9081'}<=set(values):raise ValueError()
            for key,value in values.items():
                pattern=r'(?:[0-9]{1,4}|KRX|NXT|SOR)' if key=='9081' else r'[+-]?[0-9]{1,20}(?:\.[0-9]{1,6})?'
                if not isinstance(value,str) or not re.fullmatch(pattern,value):raise ValueError()
            if not re.fullmatch('[0-9]{6}',values['20']) or not re.fullmatch('[+-]?[0-9]{1,12}',values['10']) or int(values['10'])==0:raise ValueError()
            at=datetime.strptime(self.day+' '+values['20'],'%Y-%m-%d %H%M%S').replace(tzinfo=KST)
            if not session_bounds(self.day)[0]<=at<=session_bounds(self.day)[1] or at>received:raise CaptureFailure('INVALID_PROVIDER_TIME')
            if at<previous.get(row['item'],at):raise CaptureFailure('OUT_OF_ORDER')
            previous[row['item']]=at
            candidate=not duplicate and row['item'] not in self.candidates and not any(x['code']==row['item'] for x in parsed)
            parsed.append(dict(code=row['item'],derived_at=at.isoformat(),date_basis='LOCAL_SESSION_INFERRED',candidate='CANDIDATE_FIRST_SEEN' if candidate else 'OBSERVATION',provider_exchange_raw=values['9081'],provider_exchange_interpretation='UNKNOWN',registration='ACK_OBSERVED' if self.state=='RECEIVING' else 'UNCONFIRMED'))
        self.seq+=1
        stage='FRAME' if self.state=='RECEIVING' else 'PRE_REGISTER_FRAME'
        details=json.dumps(dict(stage=stage,received_at=received.isoformat(),elapsed=now-self.started,duplicate=duplicate,rows=parsed),sort_keys=True)
        with closing(_connect(self.path)) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT INTO quote_capture_frames VALUES(?,?,?,?,?,?)',(self.session,self.seq,raw,digest,sum(x['candidate']=='CANDIDATE_FIRST_SEEN' for x in parsed),details))
            db.execute('INSERT INTO quote_capture_events VALUES(?,?,?,?,?,?)',(self.session,self.seq,stage,received.isoformat(),now-self.started,'{}'));db.commit()
        self.frames+=1;self.last=previous;self.seen.add(digest)
        self.candidates.update(x['code'] for x in parsed if x['candidate']=='CANDIDATE_FIRST_SEEN')
