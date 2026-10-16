import csv
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from app.db import ClosingConnection


def now():
    return datetime.now().isoformat(timespec='seconds')


class Database:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=20, factory=ClosingConnection)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=10000')
        return db

    def initialize(self):
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS scans(
                  scan_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  ts TEXT NOT NULL, mode TEXT NOT NULL, market TEXT NOT NULL,
                  strategy_json TEXT NOT NULL, count INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS snapshots(
                  id INTEGER PRIMARY KEY AUTOINCREMENT, scan_id INTEGER NOT NULL,
                  ts TEXT NOT NULL, code TEXT NOT NULL, name TEXT NOT NULL,
                  market TEXT NOT NULL, price REAL NOT NULL, change_pct REAL,
                  value_eok REAL, vol_ratio REAL, cap_eok REAL,
                  score REAL, passed INTEGER NOT NULL, reason TEXT NOT NULL,
                  source TEXT NOT NULL, FOREIGN KEY(scan_id) REFERENCES scans(scan_id));
                CREATE INDEX IF NOT EXISTS idx_snapshot_code_ts ON snapshots(code,ts);
                CREATE TABLE IF NOT EXISTS paper_trades(
                  id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL,
                  name TEXT NOT NULL, market TEXT NOT NULL,
                  opened_at TEXT NOT NULL, qty INTEGER NOT NULL,
                  buy_ref REAL NOT NULL, buy_fill REAL NOT NULL,
                  buy_fee REAL NOT NULL, stop_pct REAL NOT NULL,
                  take_pct REAL NOT NULL, max_days INTEGER NOT NULL,
                  highwater REAL NOT NULL, source TEXT NOT NULL,
                  strat_json TEXT NOT NULL, close_ref REAL,
                  close_fill REAL, close_fee REAL, close_tax REAL,
                  closed_at TEXT, close_reason TEXT, pnl REAL, pnl_pct REAL);
                CREATE INDEX IF NOT EXISTS idx_trades_open ON paper_trades(closed_at);
                CREATE TABLE IF NOT EXISTS proposals(
                  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
                  title TEXT NOT NULL, rationale TEXT NOT NULL,
                  patch_json TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'PENDING',
                  reviewed_at TEXT);
                CREATE TABLE IF NOT EXISTS audit(
                  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
                  kind TEXT NOT NULL, message TEXT NOT NULL);
            ''')

    def log(self, kind, message):
        with self.connect() as db:
            db.execute('INSERT INTO audit(created_at,kind,message) VALUES(?,?,?)',
                       (now(), kind, str(message)[:1500]))

    def save_scan(self, stocks, ranked, settings, market, source):
        with self.connect() as db:
            cur = db.execute('INSERT INTO scans(ts, mode, market, strategy_json,count) VALUES(?,?,?,?,?)',
                             (now(), source, market, json.dumps(settings, ensure_ascii=False), len(stocks)))
            scan_id = cur.lastrowid
            scores = {r.stock.code: r for r in ranked}
            for st in stocks:
                result = scores[st.code]
                db.execute('''INSERT INTO snapshots(scan_id,ts,code,name,market,price,change_pct,
                    value_eok,vol_ratio,cap_eok,score,passed,reason,source)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (scan_id, st.stamp or now(), st.code, st.name, st.market, st.price,
                     st.change_pct, st.trade_value_eok, st.vol_ratio_pct, st.cap_eok,
                     result.score, int(result.passed), result.reason, source))
        return scan_id

    def paper_open(self, stock, qty, stop, take, max_days, fee_pct, slippage_pct, settings, max_positions):
        if stock.price <= 0 or qty < 1:
            raise ValueError('종목 가격 또는 수량을 확인하세요.')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            n = db.execute('SELECT COUNT(*) AS n FROM paper_trades WHERE closed_at IS NULL').fetchone()['n']
            if n >= max_positions:
                raise ValueError('최대 가상 보유종목 수 제한에 도달했습니다.')
            existing = db.execute('SELECT 1 FROM paper_trades WHERE closed_at IS NULL AND code=?', (stock.code,)).fetchone()
            if existing:
                raise ValueError('이미 가상 보유 중인 종목입니다.')
            fill = stock.price * (1 + slippage_pct / 100)
            fee = fill * qty * fee_pct / 100
            db.execute('''INSERT INTO paper_trades(code,name,market,opened_at,qty,buy_ref,buy_fill,buy_fee,
                stop_pct,take_pct,max_days,highwater,source,strat_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (stock.code, stock.name, stock.market, now(), qty, stock.price, fill, fee,
                 stop, take, max_days, stock.price, stock.source, json.dumps(settings, ensure_ascii=False)))
        return fill

    def paper_close(self, trade_id, observed_price, reason, fee_pct, tax_pct, slip_pct):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            trade = db.execute('SELECT * FROM paper_trades WHERE id=? AND closed_at IS NULL', (trade_id,)).fetchone()
            if trade is None:
                raise ValueError('청산 가능한 가상 포지션이 없습니다.')
            if observed_price <= 0:
                raise ValueError('유효한 관측 가격이 없습니다.')
            fill = observed_price * (1 - slip_pct / 100)
            proceeds = fill * trade['qty']
            sell_fee = proceeds * fee_pct / 100
            sell_tax = proceeds * tax_pct / 100
            cost = trade['buy_fill'] * trade['qty'] + trade['buy_fee']
            pnl = proceeds - sell_fee - sell_tax - cost
            pnl_pct = (pnl / cost) * 100 if cost else 0
            db.execute('''UPDATE paper_trades SET close_ref=?,close_fill=?,close_fee=?,close_tax=?,
                         closed_at=?,close_reason=?,pnl=?,pnl_pct=? WHERE id=?''',
                       (observed_price, fill, sell_fee, sell_tax, now(), reason, pnl, pnl_pct, trade_id))
        return pnl

    def open_trades(self):
        with self.connect() as db:
            return [dict(x) for x in db.execute('SELECT * FROM paper_trades WHERE closed_at IS NULL ORDER BY id DESC')]

    def trades(self, limit=1000):
        with self.connect() as db:
            return [dict(x) for x in db.execute('SELECT * FROM paper_trades ORDER BY id DESC LIMIT ?', (limit,))]

    def closed_trades(self):
        with self.connect() as db:
            return [dict(x) for x in db.execute('SELECT * FROM paper_trades WHERE closed_at IS NOT NULL ORDER BY id')]

    def stats(self):
        closed = self.closed_trades()
        wins = [t for t in closed if t['pnl'] > 0]
        gross_win = sum(t['pnl'] for t in closed if t['pnl'] > 0)
        gross_loss = -sum(t['pnl'] for t in closed if t['pnl'] < 0)
        total = sum(t['pnl'] for t in closed)
        return {
            'count': len(closed), 'wins': len(wins), 'winrate': len(wins) / len(closed) * 100 if closed else 0,
            'pnl': total, 'gain': gross_win, 'loss': gross_loss,
            'profit_factor': gross_win / gross_loss if gross_loss else (float('inf') if gross_win else 0),
            'expectancy': total / len(closed) if closed else 0,
            'open_count': len(self.open_trades())
        }

    def proposals(self):
        with self.connect() as db:
            return [dict(x) for x in db.execute('SELECT * FROM proposals ORDER BY id DESC LIMIT 100')]

    def propose(self, title, rationale, patch):
        with self.connect() as db:
            # prevent identical unreviewed proposal duplication
            p = json.dumps(patch, ensure_ascii=False, sort_keys=True)
            if db.execute('SELECT 1 FROM proposals WHERE patch_json=? AND state=?', (p, 'PENDING')).fetchone():
                return False
            db.execute('INSERT INTO proposals(created_at,title,rationale,patch_json,state) VALUES(?,?,?,?,?)',
                       (now(), title, rationale, p, 'PENDING'))
            return True

    def review(self, proposal_id, accepted):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM proposals WHERE id=?', (proposal_id,)).fetchone()
            if row is None or row['state'] != 'PENDING':
                raise ValueError('대기 중인 전략 개선안이 아닙니다.')
            db.execute('UPDATE proposals SET state=?,reviewed_at=? WHERE id=?',
                       ('APPROVED' if accepted else 'REJECTED', now(), proposal_id))
            return json.loads(row['patch_json'])

    def audit_recent(self, limit=60):
        with self.connect() as db:
            return [dict(x) for x in db.execute('SELECT * FROM audit ORDER BY id DESC LIMIT ?', (limit,))]

    def export_trades(self, filename):
        rows = self.trades(100000)
        columns = ['id','code','name','market','opened_at','closed_at','qty','buy_ref','buy_fill',
                   'buy_fee','stop_pct','take_pct','close_ref','close_fill','close_fee','close_tax',
                   'close_reason','pnl','pnl_pct','source','strat_json']
        with open(filename, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=columns, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(rows)

    def export_snapshots(self, filename):
        with self.connect() as db:
            rows = [dict(r) for r in db.execute('SELECT * FROM snapshots ORDER BY id DESC')]
        cols = ['id','scan_id','ts','code','name','market','price','change_pct','value_eok',
                'vol_ratio','cap_eok','score','passed','reason','source']
        with open(filename,'w',newline='', encoding='utf-8-sig') as f:
            w = csv.DictWriter(f,fieldnames=cols)
            w.writeheader()
            w.writerows(rows)
