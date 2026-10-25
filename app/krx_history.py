"""KRX official daily market data adapter. Read-only and credential-separated.

KRX OPEN API requires a SEPARATE authorization/key and endpoint subscriptions.
No live order calls, scraping, or bundled market data are used.
"""
from __future__ import annotations
import json
import math
import sqlite3
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from app.backtest import Bar, load_ohlcv_csv
from app.db import ClosingConnection
from app.trading_clock import KST
from app.recommendation_outcomes import session_day

API_ROOT = 'https://data-dbg.krx.co.kr/svc/apis/sto/'
ENDPOINTS = {'KOSPI': 'stk_bydd_trd', 'KOSDAQ': 'ksq_bydd_trd'}


class DailyBatch(list):
    """Normalized rows plus original response count; contains no raw response."""
    def __init__(self,rows,received_count):
        super().__init__(rows)
        if type(received_count) is not int or received_count<len(self):
            raise ValueError('일별 수신 행 수를 확인해야 합니다.')
        self.received_count=received_count


def normalize_day(block,market,day,*,require_date=False):
    if not isinstance(block,list):
        raise ValueError('일별 응답 목록을 확인해야 합니다.')
    expected=session_day(day).strftime('%Y%m%d')
    if any((require_date and (not isinstance(raw,dict) or 'BAS_DD' not in raw))
           or (isinstance(raw,dict) and 'BAS_DD' in raw and raw['BAS_DD']!=expected) for raw in block):
        raise ValueError('KRX 응답의 기준일자가 요청 날짜와 다르거나 없어요. 기존 자료는 바꾸지 않았어요.')
    rows=[bar for raw in block if isinstance(raw,dict)
          if (bar:=normalize_krx(raw,market,day)) is not None]
    return DailyBatch(rows,len(block))


def validate_bars(bars):
    """Reject a malformed write batch before touching an existing cache snapshot."""
    rows=list(bars)
    seen=set()
    try:
        for row in rows:
            session_day(row.date)
            key=(row.date,row.market,row.code)
            if (not isinstance(row.code,str) or not re.fullmatch(r'[A-Za-z0-9]{6}',row.code)
                    or row.market not in ENDPOINTS or key in seen
                    or not isinstance(row.name,str) or not 1<=len(row.name)<=100):
                raise ValueError()
            values=(row.open,row.high,row.low,row.close,row.volume)
            if (not all(type(v) in (int,float) and math.isfinite(v) for v in values)
                    or min(values[:4])<=0 or row.volume<0
                    or row.low>min(row.open,row.close) or row.high<max(row.open,row.close)):
                raise ValueError()
            for value in (row.trade_value_eok,row.cap_eok):
                if value is not None and (type(value) not in (int,float) or not math.isfinite(value) or value<0):
                    raise ValueError()
            seen.add(key)
    except (AttributeError,TypeError,ValueError,OverflowError):
        raise ValueError('일별 자료의 날짜·시장·중복 종목·가격·거래대금을 확인해 주세요. 기존 자료는 바꾸지 않았어요.') from None
    return rows


def num(value):
    if value is None or str(value).strip() in ('', '-', '--'):
        return None
    try:
        x = float(str(value).replace(',', '').replace('+', '').strip())
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def normalize_krx(raw, market, expected_day):
    """KRX 'OutBlock_1' daily values in KRW and shares; convert to 100m KRW."""
    code = str(raw.get('ISU_CD', '')).strip()
    if not code:
        return None
    if not re.fullmatch(r'[A-Za-z0-9]{6}',code):
        # Preserve short codes including letters; never derive one from an ISIN.
        return None
    o, h, l, c = [num(raw.get(k)) for k in ('TDD_OPNPRC','TDD_HGPRC','TDD_LWPRC','TDD_CLSPRC')]
    v = num(raw.get('ACC_TRDVOL'))
    if None in (o,h,l,c,v) or min(o,h,l,c) <= 0 or v <= 0 or l > min(o,c) or h < max(o,c):
        return None
    val = num(raw.get('ACC_TRDVAL'))
    cap = num(raw.get('MKTCAP'))
    name = str(raw.get('ISU_NM') or code).strip()
    return Bar(expected_day,code,name,market,o,h,l,c,v,
               (val / 1e8) if val is not None else None,
               (cap / 1e8) if cap is not None else None)


class KRXDailyClient:
    def __init__(self, api_key: str, timeout=20):
        self.api_key = api_key.strip()
        self.timeout = timeout
        if not self.api_key:
            raise ValueError('KRX OPEN API 인증키를 입력하세요. 키움 API 키와 별도입니다.')

    def fetch_day(self, market: str, day: str):
        if market not in ENDPOINTS:
            raise ValueError('시장구분 오류')
        iso = date.fromisoformat(day).strftime('%Y%m%d')
        url = API_ROOT + ENDPOINTS[market] + '?' + urlencode({'basDd':iso})
        req = Request(url,headers={'AUTH_KEY':self.api_key,'Accept':'application/json','User-Agent':'KRX-Research-Desktop/0.3'})
        try:
            with urlopen(req, timeout=self.timeout) as res:
                raw = json.loads(res.read().decode('utf-8-sig'))
        except Exception as exc:
            raise RuntimeError(f'KRX {market} {day} 조회 실패 (사용승인 및 네트워크 확인): {type(exc).__name__}') from None
        block = raw.get('OutBlock_1') if isinstance(raw,dict) else None
        if not isinstance(block,list):
            raise RuntimeError('KRX 응답 형식 오류: 서비스 승인과 요청 날짜를 확인해 주세요.')
        return normalize_day(block,market,day,require_date=True)


class HistoryStore:
    def __init__(self, path):
        self.path = str(Path(path).expanduser())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def connect(self):
        db = sqlite3.connect(self.path,timeout=30,factory=ClosingConnection)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=20000')
        return db

    def _init(self):
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
            CREATE TABLE IF NOT EXISTS daily_bars(
              source TEXT NOT NULL, day TEXT NOT NULL, market TEXT NOT NULL,
              code TEXT NOT NULL, name TEXT NOT NULL,
              open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
              close REAL NOT NULL, volume REAL NOT NULL,
              trade_value_eok REAL, cap_eok REAL, fetched_at TEXT NOT NULL,
              PRIMARY KEY(source,day,market,code));
            CREATE INDEX IF NOT EXISTS ix_hist_symbol ON daily_bars(source,market,code,day);
            CREATE TABLE IF NOT EXISTS nxt_observations(
              id INTEGER PRIMARY KEY,mode TEXT NOT NULL,market TEXT NOT NULL,code TEXT NOT NULL,
              day TEXT NOT NULL,observed_at TEXT NOT NULL,eligible INTEGER CHECK(eligible=1 OR eligible IS NULL),
              UNIQUE(mode,market,code,observed_at));
            CREATE INDEX IF NOT EXISTS ix_nxt_day ON nxt_observations(mode,day,market,code);
            CREATE TABLE IF NOT EXISTS daily_ingest_status(
              source TEXT NOT NULL,market TEXT NOT NULL,day TEXT NOT NULL,
              received_count INTEGER,kept_count INTEGER NOT NULL,omitted_count INTEGER,observed_at TEXT NOT NULL,
              PRIMARY KEY(source,market,day));
            CREATE TABLE IF NOT EXISTS fetched_days(
              source TEXT NOT NULL, market TEXT NOT NULL, day TEXT NOT NULL,
              count INTEGER NOT NULL, checked_at TEXT NOT NULL,
              PRIMARY KEY(source,market,day));
            CREATE TABLE IF NOT EXISTS research_runs(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts TEXT NOT NULL, source TEXT NOT NULL, data_begin TEXT, data_end TEXT,
              universe_count INTEGER, settings_json TEXT NOT NULL,
              report_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS approved_research(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              source TEXT NOT NULL, strategy_id TEXT NOT NULL,
              params_json TEXT NOT NULL, run_id INTEGER NOT NULL,
              approved_at TEXT NOT NULL, state TEXT NOT NULL,
              UNIQUE(source,strategy_id));
            CREATE TABLE IF NOT EXISTS research_signals(
              run_id INTEGER NOT NULL, strategy TEXT NOT NULL,
              split TEXT NOT NULL, day TEXT NOT NULL, code TEXT NOT NULL,
              name TEXT NOT NULL, market TEXT NOT NULL, rank INTEGER NOT NULL,
              score REAL NOT NULL, r1 REAL, r3 REAL, r5 REAL,
              PRIMARY KEY(run_id,strategy,day,code));
            ''')
            if 'normalizer_version' not in {row['name'] for row in db.execute('PRAGMA table_info(fetched_days)')}:
                db.execute('ALTER TABLE fetched_days ADD COLUMN normalizer_version INTEGER NOT NULL DEFAULT 0')

    def insert_bars(self,bars,source):
        bars=validate_bars(bars)
        ts=datetime.now().isoformat(timespec='seconds')
        with self.connect() as db:
            db.executemany('''INSERT OR REPLACE INTO daily_bars
                (source,day,market,code,name,open,high,low,close,volume,trade_value_eok,cap_eok,fetched_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''', [
                    (source,b.date,b.market,b.code,b.name,b.open,b.high,b.low,b.close,b.volume,
                     b.trade_value_eok,b.cap_eok,ts) for b in bars])
        return len(bars)

    def load_bars(self,source,begin=None,end=None):
        query='SELECT day,code,name,market,open,high,low,close,volume,trade_value_eok,cap_eok FROM daily_bars WHERE source=?'
        args=[source]
        if begin:
            query+=' AND day>=?';args.append(begin)
        if end:
            query+=' AND day<=?';args.append(end)
        query+=' ORDER BY day,code'
        with self.connect() as db:
            return [Bar(*tuple(row)) for row in db.execute(query,args)]

    def count(self,source):
        with self.connect() as db:
            return db.execute('SELECT COUNT(*),COUNT(DISTINCT day),COUNT(DISTINCT code),MIN(day),MAX(day) FROM daily_bars WHERE source=?',(source,)).fetchone()

    def recommendation_bars(self, source, begin, end, *, now=None):
        if source != 'KRX':
            return self.load_bars(source,begin,end)
        moment=now or datetime.now(KST)
        if moment.tzinfo is None or moment.utcoffset() is None:raise ValueError('시세 확인 시각을 확인해 주세요.')
        # Reuse only snapshots fetched after their data day; same-day cache may
        # still contain intraday values even when evaluation happens tomorrow.
        with self.connect() as db:
            rows = db.execute('''SELECT b.day,b.code,b.name,b.market,b.open,b.high,b.low,b.close,
                b.volume,b.trade_value_eok,b.cap_eok FROM daily_bars b JOIN fetched_days f
                ON f.source=b.source AND f.market=b.market AND f.day=b.day
                WHERE b.source='KRX' AND b.day BETWEEN ? AND ? AND f.normalizer_version=1
                AND f.count>0 AND date(f.checked_at,'+9 hours')>f.day
                AND substr(f.checked_at,-6,1) IN ('+','-') AND substr(f.checked_at,-3,1)=':'
                AND julianday(f.checked_at)<=julianday(?)
                AND f.count=(SELECT COUNT(*) FROM daily_bars x WHERE x.source=f.source
                    AND x.market=f.market AND x.day=f.day) ORDER BY b.day,b.code''',
                (begin,end,moment.isoformat())).fetchall()
            return [Bar(*tuple(row)) for row in rows]

    def load_evidence(self,source,begin,end,*,nxt_mode=None):
        """Prices and ingest observations from the same SQLite read snapshot."""
        with self.connect() as db:
            db.execute('BEGIN')
            rows=db.execute('''SELECT day,code,name,market,open,high,low,close,volume,trade_value_eok,cap_eok
                FROM daily_bars WHERE source=? AND day BETWEEN ? AND ? ORDER BY day,code''',(source,begin,end)).fetchall()
            observations=db.execute('SELECT * FROM daily_ingest_status WHERE source=? AND day BETWEEN ? AND ?',
                                    (source,begin,end)).fetchall()
            prices=[Bar(*tuple(row)) for row in rows]
            ingest=[dict(row) for row in observations]
            if nxt_mode is None:
                return prices,ingest
            nxt=db.execute('''SELECT * FROM nxt_observations WHERE mode=? AND day=?
                ORDER BY observed_at,id''',(nxt_mode,end)).fetchall()
            return prices,ingest,[dict(row) for row in nxt]

    def ingest_summary(self,source):
        with self.connect() as db:
            rows=db.execute('SELECT * FROM daily_ingest_status WHERE source=? ORDER BY day DESC,market LIMIT 10',(source,)).fetchall()
        lines=['최근 일별 정규화 관측 · 전체 시장 수집 완료 증명 아님']
        for row in rows:
            received='미확인' if row['received_count'] is None else str(row['received_count'])
            omitted='미확인' if row['omitted_count'] is None else str(row['omitted_count'])
            lines.append(f'{row["day"]} {row["market"]} · 수신 {received} / 정규화 {row["kept_count"]} / 제외 {omitted}행')
        if not rows:lines.append('수신/제외 행 수 관측이 없어요. 저장 행 수만으로 누락 여부를 알 수 없어요.')
        lines.append('제외에는 무거래·가격 미확인·형식 차이가 포함될 수 있어요. 빈 응답은 휴장 증명이 아니에요.')
        return '\n'.join(lines)

    def import_csv(self,path,source='CSV_USER'):
        rows=load_ohlcv_csv(path)
        self.insert_bars(rows,source)
        return len(rows)

    def fetch_range(self,client,start,end,markets=('KOSPI','KOSDAQ'),limit_days=365,*,now=None):
        moment=now or datetime.now(KST)
        if not isinstance(moment,datetime) or moment.tzinfo is None:
            raise ValueError('한국 날짜를 확인할 수 있는 시각이 필요합니다.')
        today=moment.astimezone(KST).date()
        beg=date.fromisoformat(start);fin=date.fromisoformat(end)
        markets=tuple(markets)
        if (fin<beg or (fin-beg).days>limit_days or fin>today or not markets
                or len(set(markets))!=len(markets) or any(m not in ENDPOINTS for m in markets)):
            raise ValueError(f'날짜 범위가 올바르지 않습니다 (최대 {limit_days}일).')
        total=days=0
        with self.connect() as db:
            # Same-day snapshots are provisional, even after app restart. Empty
            # responses do not prove a holiday or a complete market download.
            done={(r['market'],r['day']) for r in db.execute('''SELECT f.market,f.day
                FROM fetched_days f WHERE f.source=? AND f.normalizer_version=1 AND f.count>0 AND date(f.checked_at,'+9 hours')>f.day
                AND substr(f.checked_at,-6,1) IN ('+','-') AND substr(f.checked_at,-3,1)=':'
                AND julianday(f.checked_at)<=julianday(?)
                AND f.day<? AND f.count=(SELECT COUNT(*) FROM daily_bars b
                    WHERE b.source=f.source AND b.market=f.market AND b.day=f.day)''',('KRX',moment.isoformat(),today.isoformat()))}
        d=beg
        while d<=fin:
            if d.weekday()<5:
                key=d.isoformat()
                for market in markets:
                    if (market,key) in done:
                        continue
                    rows=client.fetch_day(market,key)
                    if not isinstance(rows,(list,tuple)):
                        raise ValueError('KRX 응답의 날짜·시장·중복 종목을 확인해야 합니다.')
                    received=rows.received_count if isinstance(rows,DailyBatch) else None
                    rows=validate_bars(rows)
                    if received is not None and received<len(rows):
                        raise ValueError('일별 수신 행 수와 저장 행 수가 맞지 않습니다.')
                    if any(b.date!=key or b.market!=market for b in rows):
                        raise ValueError('KRX 응답의 날짜·시장을 확인해야 합니다.')
                    ts=moment.astimezone(KST).isoformat(timespec='seconds')
                    with self.connect() as db:
                        db.execute('BEGIN IMMEDIATE')
                        if rows:
                            db.execute('DELETE FROM daily_bars WHERE source=? AND market=? AND day=?',('KRX',market,key))
                            db.executemany('''INSERT INTO daily_bars
                                (source,day,market,code,name,open,high,low,close,volume,trade_value_eok,cap_eok,fetched_at)
                                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',[
                                ('KRX',b.date,b.market,b.code,b.name,b.open,b.high,b.low,b.close,b.volume,
                                 b.trade_value_eok,b.cap_eok,ts) for b in rows])
                            db.execute('''INSERT OR REPLACE INTO fetched_days
                                (source,market,day,count,checked_at,normalizer_version) VALUES(?,?,?,?,?,?)''',
                                ('KRX',market,key,len(rows),ts,1))
                        db.execute('INSERT OR REPLACE INTO daily_ingest_status VALUES(?,?,?,?,?,?,?)',
                                   ('KRX',market,key,received,len(rows),received-len(rows) if received is not None else None,ts))
                    total+=len(rows);days+=1
            d+=timedelta(days=1)
        return {'rows':total,'requests':days}

    def save_run(self,source,settings,report):
        if report.get('method_version')=='ROLLING_WFO_V1' and report.get('source')!=source:raise ValueError('저장 연구의 출처가 달라요.')
        encoded=json.dumps(report,ensure_ascii=False,allow_nan=False)
        stamp=datetime.now().isoformat(timespec='seconds')
        with self.connect() as db:
            cur=db.execute('INSERT INTO research_runs(ts,source,data_begin,data_end,universe_count,settings_json,report_json) VALUES(?,?,?,?,?,?,?)',
                (stamp,source,report['data_begin'],report['data_end'],report['symbols'],json.dumps(settings,ensure_ascii=False),encoded))
            rid=cur.lastrowid
            rows=[]
            for signal in report['top_signals']:
                rows.append((rid,signal['strategy'],signal['split'],signal['day'],signal['code'],signal['name'],signal['market'],signal['rank'],signal['score'],signal.get('r1'),signal.get('r3'),signal.get('r5')))
            db.executemany('''INSERT INTO research_signals(run_id,strategy,split,day,code,name,market,rank,score,r1,r3,r5)
                              VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',rows)
        return rid

    def decide_research(self,source,run_id,variant,approve,*,method="HOLDOUT_V1"):
        """Research preset ONLY. Never writes real-time Kiwoom filter or orders."""
        with self.connect() as db:
            identifier=variant['id']
            existing=db.execute('SELECT report_json FROM research_runs WHERE id=? AND source=?',(run_id,source)).fetchone()
            if existing is not None and json.loads(existing[0]).get('method_version','HOLDOUT_V1')!=method:raise ValueError('저장 결과의 연구 방법이 달라요.')
            if method=='ROLLING_WFO_V1':
                row=db.execute('SELECT report_json FROM research_runs WHERE id=? AND source=?',(run_id,source)).fetchone()
                if row is None:raise ValueError('저장 연구의 출처를 확인해 주세요.')
                report=json.loads(row[0])
                stored=next((item for item in report.get('variants',[]) if item['id']==identifier),None)
                if report.get('method_version')!=method or report.get('source')!=source or stored!=variant:raise ValueError('저장 연구의 방법·후보가 달라요.')
                if approve and (report['markets'][variant['market']]['valid_folds']<3 or report['latest_candidates'].get(variant['market'])!=identifier):
                    raise ValueError('충분한 반복평가와 최신 훈련 후보를 먼저 확인해 주세요.')
                identifier='WFO1:'+identifier
            elif method!='HOLDOUT_V1':raise ValueError('연구 방법을 확인해 주세요.')
            db.execute("""INSERT INTO approved_research(source,strategy_id,params_json,run_id,approved_at,state)
                VALUES(?,?,?,?,?,?) ON CONFLICT(source,strategy_id) DO UPDATE SET
                params_json=excluded.params_json, run_id=excluded.run_id,
                approved_at=excluded.approved_at, state=excluded.state""",
                (source,identifier,json.dumps(variant['params'],ensure_ascii=False),run_id,
                 datetime.now().isoformat(timespec='seconds'),'APPROVED_RESEARCH' if approve else 'REJECTED'))

    def research_states(self,source,*,method="HOLDOUT_V1"):
        with self.connect() as db:
            rows=db.execute('SELECT strategy_id,state FROM approved_research WHERE source=?',(source,)).fetchall()
            if method=='ROLLING_WFO_V1':return {x['strategy_id'][5:]:x['state'] for x in rows if x['strategy_id'].startswith('WFO1:')}
            if method!='HOLDOUT_V1':raise ValueError('연구 방법을 확인해 주세요.')
            return {x['strategy_id']:x['state'] for x in rows if not x['strategy_id'].startswith('WFO1:')}

    def last_run(self,source,*,method=None):
        with self.connect() as db:
            query='SELECT id,ts,report_json FROM research_runs WHERE source=? ORDER BY id DESC'
            if method is None:row=db.execute(query+' LIMIT 1',(source,)).fetchone()
            else:
                rows=db.execute(query,(source,))
                row=next((r for r in rows if json.loads(r['report_json']).get('method_version','HOLDOUT_V1')==method),None)
        return (row['id'],row['ts'],json.loads(row['report_json'])) if row else None
