"""KRX official daily market data adapter. Read-only and credential-separated.

KRX OPEN API requires a SEPARATE authorization/key and endpoint subscriptions.
No live order calls, scraping, or bundled market data are used.
"""
from __future__ import annotations
import json
import math
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from app.backtest import Bar, load_ohlcv_csv
from app.db import ClosingConnection

API_ROOT = 'https://data-dbg.krx.co.kr/svc/apis/sto/'
ENDPOINTS = {'KOSPI': 'stk_bydd_trd', 'KOSDAQ': 'ksq_bydd_trd'}


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
    if len(code) != 6 or not code.isdigit():
        # This API usually returns six-digit ticker, never invent one from an ISIN.
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
            raise RuntimeError(f'KRX {market} {day} 조회 실패 (사용승인 및 네트워크 확인): {type(exc).__name__}') from exc
        block = raw.get('OutBlock_1') if isinstance(raw,dict) else None
        if not isinstance(block,list):
            msg = raw.get('message') or raw.get('msg') or raw.get('RESULT') if isinstance(raw,dict) else 'Unexpected response'
            raise RuntimeError(f'KRX 응답 형식 오류: {str(msg)[:150]}')
        return [b for r in block if (b:=normalize_krx(r,market,day)) is not None]


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

    def insert_bars(self,bars,source):
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

    def import_csv(self,path,source='CSV_USER'):
        rows=load_ohlcv_csv(path)
        self.insert_bars(rows,source)
        return len(rows)

    def fetch_range(self,client,start,end,markets=('KOSPI','KOSDAQ'),limit_days=365):
        beg=date.fromisoformat(start);fin=date.fromisoformat(end)
        if fin<beg or (fin-beg).days>limit_days or fin>date.today():
            raise ValueError(f'날짜 범위가 올바르지 않습니다 (최대 {limit_days}일).')
        total=days=0
        with self.connect() as db:
            done={(r['market'],r['day']) for r in db.execute('SELECT market,day FROM fetched_days WHERE source=?',('KRX',))}
        d=beg
        while d<=fin:
            if d.weekday()<5:
                key=d.isoformat()
                for market in markets:
                    if (market,key) in done:
                        continue
                    rows=client.fetch_day(market,key)
                    if rows:
                        self.insert_bars(rows,'KRX')
                    # Current-day empty response may be pending; retry next time.
                    if rows or d<date.today():
                        with self.connect() as db:
                            db.execute('INSERT OR REPLACE INTO fetched_days VALUES(?,?,?,?,?)',
                                       ('KRX',market,key,len(rows),datetime.now().isoformat(timespec='seconds')))
                    total+=len(rows);days+=1
            d+=timedelta(days=1)
        return {'rows':total,'requests':days}

    def save_run(self,source,settings,report):
        stamp=datetime.now().isoformat(timespec='seconds')
        with self.connect() as db:
            cur=db.execute('INSERT INTO research_runs(ts,source,data_begin,data_end,universe_count,settings_json,report_json) VALUES(?,?,?,?,?,?,?)',
                (stamp,source,report['data_begin'],report['data_end'],report['symbols'],json.dumps(settings,ensure_ascii=False),json.dumps(report,ensure_ascii=False)))
            rid=cur.lastrowid
            rows=[]
            for signal in report['top_signals']:
                rows.append((rid,signal['strategy'],signal['split'],signal['day'],signal['code'],signal['name'],signal['market'],signal['rank'],signal['score'],signal.get('r1'),signal.get('r3'),signal.get('r5')))
            db.executemany('''INSERT INTO research_signals(run_id,strategy,split,day,code,name,market,rank,score,r1,r3,r5)
                              VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',rows)
        return rid

    def decide_research(self,source,run_id,variant,approve):
        """Research preset ONLY. Never writes real-time Kiwoom filter or orders."""
        with self.connect() as db:
            db.execute("""INSERT INTO approved_research(source,strategy_id,params_json,run_id,approved_at,state)
                VALUES(?,?,?,?,?,?) ON CONFLICT(source,strategy_id) DO UPDATE SET
                params_json=excluded.params_json, run_id=excluded.run_id,
                approved_at=excluded.approved_at, state=excluded.state""",
                (source,variant['id'],json.dumps(variant['params'],ensure_ascii=False),run_id,
                 datetime.now().isoformat(timespec='seconds'),'APPROVED_RESEARCH' if approve else 'REJECTED'))

    def research_states(self,source):
        with self.connect() as db:
            return {x['strategy_id']:x['state'] for x in db.execute(
                'SELECT strategy_id,state FROM approved_research WHERE source=?',(source,))}

    def last_run(self,source):
        with self.connect() as db:
            row=db.execute('SELECT id,ts,report_json FROM research_runs WHERE source=? ORDER BY id DESC LIMIT 1',(source,)).fetchone()
        return (row['id'],row['ts'],json.loads(row['report_json'])) if row else None
