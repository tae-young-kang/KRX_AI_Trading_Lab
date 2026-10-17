"""Versioned, independent shadow-strategy paper accounts. NEVER routes real orders.

All fills are *hypothetical*: observed scan last-price plus configurable slippage,
not a limit-order-book fill simulation. Runs only when fresh scans arrive.
"""
from __future__ import annotations
import csv
import json
from datetime import datetime
from math import isfinite
from dataclasses import dataclass
from app.strategy import evaluate
from app.storage import now

# Fields that may be promoted to an active user screener strategy.
STRATEGY_KEYS = frozenset({
    'market', 'include_etf', 'min_price', 'max_price', 'min_cap_eok', 'max_cap_eok',
    'min_value_eok', 'min_vol_ratio', 'min_change_pct', 'max_change_pct',
    'on_price', 'on_cap', 'on_value', 'on_volratio', 'on_change',
    'exit_stop_pct', 'exit_take_pct', 'exit_max_days',
    'max_paper_positions', 'paper_fee_pct', 'paper_sell_tax_pct',
    'paper_slippage_pct', 'backtest_gap_limit',
})


def strategy_fields(config):
    return {k: config[k] for k in sorted(STRATEGY_KEYS) if k in config}


def seed_variants(cfg):
    """Fixed experiment set: previous-DAY volume ratio (NOT trailing 20d avg)."""
    out=[]
    for mrkt in ('KOSPI','KOSDAQ'):
        for threshold in (100,120,150,200,300):
            c=dict(cfg)
            c.update(market=mrkt,on_volratio=True,min_vol_ratio=float(threshold))
            out.append((f'{mrkt} · 전일 거래량비 {threshold}%',c,
                        '키움 시세의 전일 거래량비. 20일 평균 기반 CSV 백테스트와 별개.'))
    return out


@dataclass
class ShadowEvent:
    strategy_id: int
    name: str
    code: str
    event: str
    pnl: float | None = None


class ShadowEngine:
    def __init__(self, database):
        self.db = database
        self._init_tables()

    def _init_tables(self):
        with self.db.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS shadow_strategies(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL, created_at TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'SHADOW',
              strategy_json TEXT NOT NULL,
              note TEXT NOT NULL DEFAULT '',
              origin TEXT NOT NULL DEFAULT 'SEED',
              initial_cash REAL NOT NULL,
              qty INTEGER NOT NULL DEFAULT 1,
              source_proposal_id INTEGER, data_source TEXT NOT NULL DEFAULT 'DEMO');
            CREATE TABLE IF NOT EXISTS shadow_trades(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              strategy_id INTEGER NOT NULL, code TEXT NOT NULL,
              name TEXT NOT NULL, market TEXT NOT NULL, source TEXT NOT NULL,
              opened_at TEXT NOT NULL, qty INTEGER NOT NULL,
              buy_ref REAL NOT NULL, buy_fill REAL NOT NULL,
              buy_fee REAL NOT NULL, entry_cost REAL NOT NULL,
              stop_pct REAL NOT NULL, take_pct REAL NOT NULL,
              max_days INTEGER NOT NULL,
              close_ref REAL, close_fill REAL, sell_fee REAL,
              sell_tax REAL, closed_at TEXT, close_reason TEXT,
              pnl REAL, pnl_pct REAL,
              FOREIGN KEY(strategy_id) REFERENCES shadow_strategies(id));
            CREATE INDEX IF NOT EXISTS idx_shadow_trade_live ON shadow_trades(strategy_id, closed_at);
            CREATE TABLE IF NOT EXISTS shadow_marks(
              strategy_id INTEGER NOT NULL, code TEXT NOT NULL,
              price REAL NOT NULL, asof TEXT NOT NULL,
              PRIMARY KEY(strategy_id, code));
            CREATE TABLE IF NOT EXISTS shadow_signals(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              scan_id INTEGER NOT NULL, strategy_id INTEGER NOT NULL,
              code TEXT NOT NULL, name TEXT NOT NULL, market TEXT NOT NULL,
              price REAL NOT NULL, score REAL NOT NULL,
              selected INTEGER NOT NULL, reason TEXT NOT NULL,
              UNIQUE(scan_id, strategy_id, code));
            CREATE TABLE IF NOT EXISTS shadow_daily(
              strategy_id INTEGER NOT NULL, day TEXT NOT NULL,
              equity REAL NOT NULL, realized_pnl REAL NOT NULL,
              unrealized_pnl REAL NOT NULL, closed_count INTEGER NOT NULL,
              open_count INTEGER NOT NULL, stale_marks INTEGER NOT NULL,
              updated_at TEXT NOT NULL, source TEXT NOT NULL,
              PRIMARY KEY(strategy_id, day));
            CREATE TABLE IF NOT EXISTS shadow_processed_scans(
              scan_id INTEGER PRIMARY KEY, processed_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS shadow_pending(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              strategy_id INTEGER NOT NULL, code TEXT NOT NULL, name TEXT NOT NULL,
              signal_day TEXT NOT NULL, signal_price REAL NOT NULL,
              status TEXT NOT NULL DEFAULT 'PENDING', resolved_day TEXT,
              reason TEXT, buy_observed REAL, UNIQUE(strategy_id,code,signal_day));
            CREATE INDEX IF NOT EXISTS idx_shadow_pending ON shadow_pending(strategy_id,status);
            ''')
            columns={r['name'] for r in db.execute('PRAGMA table_info(shadow_strategies)')}
            if 'data_source' not in columns:
                db.execute("ALTER TABLE shadow_strategies ADD COLUMN data_source TEXT NOT NULL DEFAULT 'DEMO'")

    def strategies(self, include_stopped=False):
        with self.db.connect() as db:
            where = '' if include_stopped else "WHERE status != 'STOPPED'"
            return [dict(x) for x in db.execute(
                f'SELECT * FROM shadow_strategies {where} ORDER BY id')]

    def add(self, name, cfg, note='', origin='USER', initial_cash=1_000_000,
            qty=1, source_proposal_id=None, data_source='DEMO'):
        if initial_cash <= 0 or qty < 1:
            raise ValueError('가상계좌의 초기 투자금과 수량은 양수여야 합니다.')
        filtered = strategy_fields(cfg)
        if not filtered or 'market' not in filtered:
            raise ValueError('매매 조건이 잘못되었습니다.')
        with self.db.connect() as db:
            cur = db.execute('''INSERT INTO shadow_strategies(
                name,created_at,status,strategy_json,note,origin,initial_cash,qty,source_proposal_id,data_source)
                VALUES(?,?,?,?,?,?,?,?,?,?)''',
                (name[:100], now(), 'SHADOW', json.dumps(filtered, sort_keys=True, ensure_ascii=False),
                 note[:500], origin, float(initial_cash), int(qty), source_proposal_id,data_source))
            return cur.lastrowid

    def initialize_defaults(self, cfg, data_source='DEMO'):
        existing=[s for s in self.strategies(include_stopped=True) if s['data_source']==data_source]
        if existing: return 0
        self.add(f'현재 조건 기준선 ({data_source})',cfg,'기존 조건 비교 기준선',
                 'BASELINE',data_source=data_source)
        for name,variant,note in seed_variants(cfg):
            self.add(name,variant,note,'SEED',data_source=data_source)
        return 1+len(seed_variants(cfg))

    def ensure_active_reference(self, cfg, data_source='DEMO'):
        signature=strategy_fields(cfg)
        for row in self.strategies(include_stopped=True):
            if row['origin']=='ACTIVE' and row['data_source']==data_source and json.loads(row['strategy_json'])==signature:
                return row['id']
        return self.add(f'승인 조건 v{cfg.get("strategy_version",1)} ({data_source})',cfg,
                        '승인 당시 조건을 버전 고정', 'ACTIVE',data_source=data_source)

    def add_proposal_variant(self, cfg, proposal_id, data_source='DEMO'):
        for s in self.strategies(include_stopped=True):
            if s['source_proposal_id']==proposal_id and s['data_source']==data_source:
                return s['id']
        return self.add(f'미적용 개선안 #{proposal_id}',cfg,
                        '실제 주문·승인 없이 별도 가상 추적','PROPOSAL',
                        source_proposal_id=proposal_id,data_source=data_source)

    def stop(self, strategy_id):
        with self.db.connect() as db:
            db.execute("UPDATE shadow_strategies SET status='STOPPED' WHERE id=?", (strategy_id,))

    def resume(self, strategy_id):
        with self.db.connect() as db:
            db.execute("UPDATE shadow_strategies SET status='SHADOW' WHERE id=?", (strategy_id,))

    def _get_holdings(self, db, strategy_id):
        return db.execute('SELECT * FROM shadow_trades WHERE strategy_id=? AND closed_at IS NULL',
                          (strategy_id,)).fetchall()

    def _cash(self, db, strategy_id, initial):
        # Already buys include buy commission, sells are net proceeds.
        row = db.execute('''SELECT COALESCE(SUM(entry_cost),0) buys,
            COALESCE(SUM(CASE WHEN closed_at IS NOT NULL THEN
               close_fill * qty - sell_fee - sell_tax ELSE 0 END),0) sells
            FROM shadow_trades WHERE strategy_id=?''', (strategy_id,)).fetchone()
        return initial - row['buys'] + row['sells']

    def _mark_snapshot(self, db, strategy, latest, day):
        sid = strategy['id']
        holds = self._get_holdings(db, sid)
        config = json.loads(strategy['strategy_json'])
        fee = config.get('paper_fee_pct', 0.015)/100
        tax = config.get('paper_sell_tax_pct', 0.2)/100
        slippage = config.get('paper_slippage_pct', 0.1)/100
        unrealized = 0.0
        stale = 0
        for h in holds:
            market = latest.get(h['code'])
            if market is not None:
                px = market.price
                db.execute('''INSERT INTO shadow_marks(strategy_id,code,price,asof)
                    VALUES(?,?,?,?) ON CONFLICT(strategy_id,code)
                    DO UPDATE SET price=excluded.price,asof=excluded.asof''',
                    (sid,h['code'],px,market.stamp or now()))
            else:
                saved = db.execute('SELECT price,asof FROM shadow_marks WHERE strategy_id=? AND code=?',
                                   (sid,h['code'])).fetchone()
                if saved is None:
                    stale += 1
                    px = h['buy_ref']
                else:
                    stale += 1
                    px = saved['price']
            # Conservative estimated liquidation value; not a real executable quote.
            sell_fill = px*(1-slippage)
            unrealized += sell_fill*h['qty']*(1-fee-tax) - h['entry_cost']
        closed = db.execute('''SELECT COUNT(*) n,COALESCE(SUM(pnl),0) pnl FROM shadow_trades
                               WHERE strategy_id=? AND closed_at IS NOT NULL''', (sid,)).fetchone()
        equity = strategy['initial_cash'] + closed['pnl'] + unrealized
        sources = sorted({s.source for s in latest.values()})
        db.execute('''INSERT INTO shadow_daily(strategy_id,day,equity,realized_pnl,
          unrealized_pnl,closed_count,open_count,stale_marks,updated_at,source)
          VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(strategy_id,day) DO UPDATE SET
          equity=excluded.equity, realized_pnl=excluded.realized_pnl,
          unrealized_pnl=excluded.unrealized_pnl,closed_count=excluded.closed_count,
          open_count=excluded.open_count,stale_marks=excluded.stale_marks,
          updated_at=excluded.updated_at,source=excluded.source''',
          (sid,day,equity,closed['pnl'],unrealized,closed['n'],len(holds),stale,now(),
           ','.join(sources) or 'UNKNOWN'))

    def process_scan(self, scan_id, stocks, today=None):
        """Idempotent paper experiment; buy at first observed scan of later day.

        This is NOT NXT 08:00 or actual opening-price execution.
        Missing quotes => no entry/exit, stale mark on the dashboard.
        """
        day=today or datetime.now().date().isoformat()
        latest={s.code:s for s in stocks if s.price>0 and isfinite(s.price)}
        if not latest:
            return []
        output=[]
        with self.db.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM shadow_processed_scans WHERE scan_id=?',(scan_id,)).fetchone():
                return []
            meta=db.execute('SELECT mode,market FROM scans WHERE scan_id=?',(scan_id,)).fetchone()
            if meta is None:
                raise ValueError('먼저 스캔을 저장해야 합니다.')
            source=meta['mode']
            for strategy in [s for s in self.strategies() if s['data_source']==source]:
                sid=strategy['id'];cfg=json.loads(strategy['strategy_json'])
                if meta['market']!='전체' and cfg['market']!='전체' and meta['market']!=cfg['market']:
                    continue
                quotes={code:st for code,st in latest.items()
                        if st.source==source and (cfg['market']=='전체' or st.market==cfg['market'])}
                # 1) Manage existing open paper positions on new observed quotes.
                for holding in self._get_holdings(db,sid):
                    st=quotes.get(holding['code'])
                    if st is None or holding['source']!=source: continue
                    age=(datetime.fromisoformat(day).date()-datetime.fromisoformat(holding['opened_at']).date()).days
                    move=100*(st.price/holding['buy_fill']-1)
                    reason=('손절' if move<=-holding['stop_pct'] else
                            '익절' if move>=holding['take_pct'] else
                            '보유기간' if age>=holding['max_days'] else None)
                    if reason:
                        slip=cfg.get('paper_slippage_pct',0.1)/100
                        fee=cfg.get('paper_fee_pct',0.015)/100
                        tax=cfg.get('paper_sell_tax_pct',0.2)/100
                        fill=st.price*(1-slip)
                        proceeds=fill*holding['qty']
                        sell_fee=proceeds*fee; sell_tax=proceeds*tax
                        pnl=proceeds-sell_fee-sell_tax-holding['entry_cost']
                        db.execute('''UPDATE shadow_trades SET close_ref=?,close_fill=?,
                            sell_fee=?,sell_tax=?,closed_at=?,close_reason=?,pnl=?,pnl_pct=? WHERE id=?''',
                            (st.price,fill,sell_fee,sell_tax,now(),reason,pnl,
                             100*pnl/holding['entry_cost'],holding['id']))
                        output.append(ShadowEvent(sid,strategy['name'],st.code,'EXIT_'+reason,pnl))
                # 2) Previous-day signal may enter at FIRST OBSERVED later-day scan price.
                #    Check gap before buying. We never backfill an unknown quote.
                pending=db.execute('''SELECT * FROM shadow_pending WHERE strategy_id=?
                  AND status='PENDING' AND signal_day<? ORDER BY id''',(sid,day)).fetchall()
                holdings=self._get_holdings(db,sid)
                held_codes={r['code'] for r in holdings}
                cash=self._cash(db,sid,strategy['initial_cash'])
                max_open=max(1,int(cfg.get('max_paper_positions',3)))
                for pend in pending:
                    code=pend['code'];st=quotes.get(code)
                    if st is None:
                        db.execute('''UPDATE shadow_pending SET status='SKIPPED',
                          resolved_day=?,reason=? WHERE id=?''',
                          (day,'다음 관측일 후보군에서 시세 미수집',pend['id']))
                        continue
                    gap=100*(st.price/pend['signal_price']-1)
                    reason=None
                    if gap>float(cfg.get('backtest_gap_limit',3)):
                        reason='갭상승 상한 초과'
                    elif code in held_codes:
                        reason='기보유 종목'
                    elif db.execute('''SELECT 1 FROM shadow_trades WHERE
                       strategy_id=? AND code=? AND substr(opened_at,1,10)=?''',
                       (sid,code,day)).fetchone():
                        reason='당일 재진입 제한'
                    elif len(held_codes)>=max_open:
                        reason='보유종목 수 제한'
                    qty=strategy['qty']
                    fill=st.price*(1+cfg.get('paper_slippage_pct',0.1)/100)
                    buy_fee=fill*qty*cfg.get('paper_fee_pct',0.015)/100
                    entry_cost=fill*qty+buy_fee
                    if not reason and entry_cost>cash:
                        reason='가상 잔액 부족'
                    if reason:
                        db.execute('''UPDATE shadow_pending SET status='SKIPPED',
                          resolved_day=?,reason=?,buy_observed=? WHERE id=?''',
                          (day,reason,st.price,pend['id']))
                        continue
                    db.execute('''INSERT INTO shadow_trades(strategy_id,code,name,market,source,opened_at,
                        qty,buy_ref,buy_fill,buy_fee,entry_cost,stop_pct,take_pct,max_days)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                        (sid,code,st.name,st.market,source,day,qty,st.price,
                         fill,buy_fee,entry_cost,cfg['exit_stop_pct'],cfg['exit_take_pct'],
                         cfg['exit_max_days']))
                    db.execute('''UPDATE shadow_pending SET status='FILLED',
                      resolved_day=?,reason=?,buy_observed=? WHERE id=?''',
                      (day,'다음 관측일 시세 · 시가 아님',st.price,pend['id']))
                    held_codes.add(code);cash-=entry_cost
                    output.append(ShadowEvent(sid,strategy['name'],code,'BUY'))
                # 3) Today's filter signals become pending for a LATER day.
                ranked=evaluate(list(quotes.values()),cfg)
                for result in ranked:
                    st=result.stock
                    db.execute('''INSERT OR IGNORE INTO shadow_signals(
                      scan_id,strategy_id,code,name,market,price,score,selected,reason)
                      VALUES(?,?,?,?,?,?,?,?,?)''',
                      (scan_id,sid,st.code,st.name,st.market,st.price,result.score,
                       int(result.passed),result.reason))
                    if result.passed:
                        db.execute('''INSERT OR IGNORE INTO shadow_pending(
                           strategy_id,code,name,signal_day,signal_price) VALUES(?,?,?,?,?)''',
                           (sid,st.code,st.name,day,st.price))
                self._mark_snapshot(db,strategy,quotes,day)
            db.execute('INSERT INTO shadow_processed_scans(scan_id,processed_at) VALUES(?,?)',
                       (scan_id,now()))
        return output

    def pending_history(self, strategy_id=None):
        with self.db.connect() as db:
            if strategy_id is None:
                rows=db.execute('SELECT * FROM shadow_pending ORDER BY id DESC')
            else:
                rows=db.execute('SELECT * FROM shadow_pending WHERE strategy_id=? ORDER BY id DESC',(strategy_id,))
            return [dict(r) for r in rows]

    def standings(self):
        result = []
        with self.db.connect() as db:
            for s in self.strategies(include_stopped=True):
                sid = s['id']
                count = db.execute('''SELECT COUNT(*) n,
                    COALESCE(SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END),0) wins
                    FROM shadow_trades WHERE strategy_id=? AND closed_at IS NOT NULL''',(sid,)).fetchone()
                latest = db.execute('''SELECT * FROM shadow_daily WHERE strategy_id=?
                    ORDER BY day DESC LIMIT 1''',(sid,)).fetchone()
                curve = db.execute('''SELECT equity FROM shadow_daily WHERE strategy_id=?
                    ORDER BY day''',(sid,)).fetchall()
                high = s['initial_cash']
                mdd = 0.0
                for point in curve:
                    high = max(high,point['equity'])
                    mdd = min(mdd,(point['equity']/high-1)*100)
                equity = latest['equity'] if latest else s['initial_cash']
                result.append({**s,'closed':count['n'],'wins':count['wins'],
                    'winrate':100*count['wins']/count['n'] if count['n'] else 0,
                    'equity':equity,'return_pct':100*(equity/s['initial_cash']-1),
                    'mdd':mdd,'realized':latest['realized_pnl'] if latest else 0,
                    'unrealized':latest['unrealized_pnl'] if latest else 0,
                    'open':latest['open_count'] if latest else 0,
                    'stale':latest['stale_marks'] if latest else 0,
                    'last_day':latest['day'] if latest else '—',
                    'observation_days':len(curve)})
        return result

    def daily_history(self, strategy_id=None):
        with self.db.connect() as db:
            if strategy_id is not None:
                rows = db.execute('SELECT * FROM shadow_daily WHERE strategy_id=? ORDER BY day',
                                  (strategy_id,))
            else:
                rows = db.execute('SELECT * FROM shadow_daily ORDER BY day,strategy_id')
            return [dict(r) for r in rows]

    def trade_history(self, strategy_id=None):
        with self.db.connect() as db:
            if strategy_id is None:
                rows = db.execute('SELECT * FROM shadow_trades ORDER BY id DESC')
            else:
                rows = db.execute('SELECT * FROM shadow_trades WHERE strategy_id=? ORDER BY id DESC',
                                  (strategy_id,))
            return [dict(r) for r in rows]

    def signal_history(self, strategy_id=None):
        with self.db.connect() as db:
            if strategy_id is None:
                rows = db.execute('SELECT * FROM shadow_signals ORDER BY id DESC')
            else:
                rows = db.execute('SELECT * FROM shadow_signals WHERE strategy_id=? ORDER BY id DESC',
                                  (strategy_id,))
            return [dict(r) for r in rows]

    def export(self, root):
        """Create three BOM UTF-8 CSV files in a user-chosen directory."""
        from pathlib import Path
        root = Path(root)
        root.mkdir(parents=True,exist_ok=True)
        strategies = {s['id']:s for s in self.strategies(include_stopped=True)}
        files = []
        sets = [
            ('shadow_daily.csv', self.daily_history(),
             ['strategy_id','strategy_name','day','equity','realized_pnl','unrealized_pnl',
              'closed_count','open_count','stale_marks','updated_at','source']),
            ('shadow_trades.csv', self.trade_history(),
             ['id','strategy_id','strategy_name','code','name','market','source','opened_at',
              'qty','buy_ref','buy_fill','buy_fee','entry_cost','stop_pct','take_pct',
              'max_days','close_ref','close_fill','sell_fee','sell_tax','closed_at',
              'close_reason','pnl','pnl_pct']),
            ('shadow_pending.csv', self.pending_history(),
             ['id','strategy_id','strategy_name','code','name','signal_day','signal_price',
              'status','resolved_day','reason','buy_observed']),
            ('shadow_signals.csv', self.signal_history(),
             ['id','scan_id','strategy_id','strategy_name','code','name','market',
              'price','score','selected','reason'])
        ]
        for filename, rows, cols in sets:
            for r in rows:
                r['strategy_name'] = strategies.get(r['strategy_id'],{}).get('name','')
            path = root/filename
            with path.open('w',newline='',encoding='utf-8-sig') as f:
                writer = csv.DictWriter(f,fieldnames=cols,extrasaction='ignore')
                writer.writeheader()
                writer.writerows(rows)
            files.append(path)
        return files

    def summarize_comparison(self, minimum_days=5, minimum_trades=10):
        """Descriptive comparison ONLY. No automatic promotion or profitability claims."""
        data = self.standings()
        eligible = [s for s in data if s['observation_days']>=minimum_days and s['closed']>=minimum_trades
                    and s['status']!='STOPPED' and s['stale']==0]
        if len(eligible)<2:
            return f'추가 검증 필요 · 비교 가능 전략 {len(eligible)}개 (최소 {minimum_days}관측일/{minimum_trades}청산건/최신시세).'
        best = max(eligible,key=lambda x:(x['return_pct'],x['mdd']))
        return (f'관찰 성과 상위: {best["name"]} · 가상 자산수익률 {best["return_pct"]:+.2f}% · '
                f'MDD {best["mdd"]:.2f}% · 청산 {best["closed"]}건. '
                '전략별 시작일/시장 노출이 다를 수 있어 수익성 우위는 미확정.')
