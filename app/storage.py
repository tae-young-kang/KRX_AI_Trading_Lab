import csv
import json
import sqlite3
import hashlib
import re
from math import isfinite, fsum
from datetime import datetime
from pathlib import Path
from app.db import ClosingConnection
from app.trade_review import settings_snapshot, review_conditions, outcome, SNAPSHOT_KEYS
from app import recommendations
from app import recommendation_overview
from app import paper_exits
from app.trading_clock import KST


def now():
    return datetime.now(KST).isoformat(timespec='seconds')


class Database:
    def __init__(self, path,*,read_only=False):
        self.path = Path(path)
        self.read_only=read_only is True
        if self.read_only:return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self):
        db = sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro' if self.read_only else str(self.path),
                             uri=self.read_only,timeout=20,factory=ClosingConnection)
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
                CREATE INDEX IF NOT EXISTS idx_snapshot_scan ON snapshots(scan_id);
                CREATE TABLE IF NOT EXISTS scan_metrics(
                    scan_id INTEGER PRIMARY KEY,elapsed_seconds REAL,http_calls INTEGER,fallbacks INTEGER);
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
                CREATE TABLE IF NOT EXISTS proposal_context(
                  proposal_id INTEGER PRIMARY KEY,mode TEXT NOT NULL,base_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS audit(
                  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
                  kind TEXT NOT NULL, message TEXT NOT NULL);
            ''')
            columns = {r['name'] for r in db.execute('PRAGMA table_info(paper_trades)')}
            for column in ('entry_json', 'exit_settings_json','entry_key','entry_digest'):
                if column not in columns:
                    db.execute(f'ALTER TABLE paper_trades ADD COLUMN {column} TEXT')
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS ix_paper_entry_key ON paper_trades(entry_key)')
            recommendations.initialize(db)
            paper_exits.initialize(db)

    def log(self, kind, message):
        with self.connect() as db:
            db.execute('INSERT INTO audit(created_at,kind,message) VALUES(?,?,?)',
                       (now(), kind, str(message)[:1500]))

    def save_scan(self, stocks, ranked, settings, market, source, *, metrics=None):
        if metrics is not None:
            if (not isinstance(metrics,dict) or set(metrics)!={'completed','elapsed_seconds','http_calls','fallbacks'}
                    or metrics['completed'] is not True or type(metrics['elapsed_seconds']) not in (int,float)
                    or not isfinite(metrics['elapsed_seconds']) or not 0<=metrics['elapsed_seconds']<=86400
                    or any(type(metrics[key]) is not int or not 0<=metrics[key]<=100000 for key in ('http_calls','fallbacks'))):
                raise ValueError('완료된 검색의 시간·요청 집계를 확인해 주세요.')
        stocks,ranked = list(stocks),list(ranked)
        recommendations.validate_population(stocks,ranked)
        with self.connect() as db:
            ts = datetime.now(KST).isoformat(timespec='seconds')
            cur = db.execute('INSERT INTO scans(ts, mode, market, strategy_json,count) VALUES(?,?,?,?,?)',
                             (ts, source, market, json.dumps(settings_snapshot(settings), ensure_ascii=False), len(stocks)))
            scan_id = cur.lastrowid
            if metrics is not None:
                db.execute('INSERT INTO scan_metrics VALUES(?,?,?,?)',
                           (scan_id,metrics['elapsed_seconds'],metrics['http_calls'],metrics['fallbacks']))
            recommendations.record(db,scan_id,ranked,settings,source,ts)
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

    def recommendation_history(self, mode, limit=50):
        with self.connect() as db:
            return recommendations.recent(db,mode,limit)

    def review_recommendation(self, **evidence):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            return recommendations.review(db,**evidence)

    def recommendation_summary(self, mode, *, performance=False,page=1):
        with self.connect() as db:
            db.execute('BEGIN')
            return recommendations.summary(db,mode,performance=performance,page=page)

    def recommendation_overview(self, mode):
        with self.connect() as db:
            db.execute('BEGIN')
            return recommendation_overview.summary(db,mode)

    def recommendation_search_result(self, mode, scan_id, *, page=1, details=False):
        with self.connect() as db:
            db.execute('BEGIN')
            return recommendations.search_result(db,mode,scan_id,page=page,details=details)

    def recommendation_page_count(self, mode):
        with self.connect() as db:
            return recommendations.page_count(db,mode)

    def paper_open(self, stock, qty, stop, take, max_days, fee_pct, slippage_pct, settings, max_positions,
                   *,entry_key=None,observed_at=None,venue=None,price_basis='OBSERVED_TRADE',evidence_hash=None):
        if (type(qty) is not int or qty<1 or type(stock.price) not in (int,float)
                or not isfinite(stock.price) or stock.price<=0
                or not all(type(v) in (int,float) and isfinite(v) and 0<=v<100 for v in (fee_pct,slippage_pct))):
            raise ValueError('종목 가격 또는 수량을 확인하세요.')
        if observed_at is not None and (not isinstance(observed_at,datetime) or observed_at.tzinfo is None or observed_at.utcoffset() is None):
            raise ValueError('가상 진입 근거의 시간대를 확인해 주세요.')
        if entry_key is not None and (not isinstance(entry_key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',entry_key) or observed_at is None):
            raise ValueError('고정된 가상 진입 요청 번호와 관측 시각이 필요해요.')
        if venue is not None and venue not in ('KRX','NXT'):raise ValueError('가상 진입 시장을 확인해 주세요.')
        if price_basis not in ('OBSERVED_TRADE','VENUE_FIRST_TRADE'):raise ValueError('가상 진입 가격 기준을 확인해 주세요.')
        if evidence_hash is not None and (not isinstance(evidence_hash,str) or not re.fullmatch('[a-f0-9]{64}',evidence_hash)):
            raise ValueError('가상 진입 가격의 근거 해시를 확인해 주세요.')
        opened=observed_at.astimezone(KST).isoformat() if observed_at is not None else now()
        digest=hashlib.sha256(json.dumps(dict(code=stock.code,source=stock.source,price=stock.price,qty=qty,
            stop=stop,take=take,max_days=max_days,fee=fee_pct,slip=slippage_pct,
            settings=settings_snapshot(settings),opened=opened,venue=venue,basis=price_basis,evidence=evidence_hash),sort_keys=True).encode()).hexdigest()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if entry_key is not None:
                prior=db.execute('SELECT entry_digest,buy_fill FROM paper_trades WHERE entry_key=?',(entry_key,)).fetchone()
                if prior:
                    if prior['entry_digest']!=digest:raise ValueError('같은 가상 진입 요청의 내용이 달라요. 중복 매수하지 않았어요.')
                    return prior['buy_fill']
            n = db.execute('SELECT COUNT(*) AS n FROM paper_trades WHERE closed_at IS NULL').fetchone()['n']
            if n >= max_positions:
                raise ValueError('최대 가상 보유종목 수 제한에 도달했습니다.')
            existing = db.execute('SELECT 1 FROM paper_trades WHERE closed_at IS NULL AND code=?', (stock.code,)).fetchone()
            if existing:
                raise ValueError('이미 가상 보유 중인 종목입니다.')
            fill = stock.price * (1 + slippage_pct / 100)
            fee = fill * qty * fee_pct / 100
            if not isfinite(fill) or not isfinite(fee) or not isfinite(fill*qty+fee):
                raise ValueError('연습 매수 금액의 계산 범위를 벗어났어요.')
            snapshot = settings_snapshot(settings)
            snapshot.update(exit_stop_pct=stop, exit_take_pct=take, exit_max_days=max_days,
                            paper_fee_pct=fee_pct, paper_slippage_pct=slippage_pct)
            entry = {k: getattr(stock, k, None) for k in
                     ('price', 'change_pct', 'trade_value_eok', 'vol_ratio_pct', 'cap_eok',
                      'volume', 'source', 'stamp', 'quote_stamp', 'is_etf')}
            if venue is not None:entry.update(venue=venue,price_basis=price_basis)
            if evidence_hash is not None:entry['evidence_hash']=evidence_hash
            db.execute('''INSERT INTO paper_trades(code,name,market,opened_at,qty,buy_ref,buy_fill,buy_fee,
                stop_pct,take_pct,max_days,highwater,source,strat_json,entry_json,remaining_qty,realized_pnl,entry_key,entry_digest)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (stock.code, stock.name, stock.market, opened, qty, stock.price, fill, fee,
                 stop, take, max_days, stock.price, stock.source, json.dumps(snapshot, ensure_ascii=False),
                 json.dumps(entry, ensure_ascii=False),qty,0,entry_key,digest if entry_key is not None else None))
        return fill

    def paper_close(self, trade_id, observed_price, reason, fee_pct, tax_pct, slip_pct, *, request_key=None,observed_at=None):
        return self.paper_sell(trade_id,None,observed_price,reason,fee_pct,tax_pct,slip_pct,request_key=request_key,observed_at=observed_at)

    def paper_sell(self, trade_id, quantity, observed_price, reason, fee_pct, tax_pct, slip_pct, *, request_key=None,observed_at=None):
        if observed_at is not None and (not isinstance(observed_at,datetime) or observed_at.tzinfo is None or observed_at.utcoffset() is None):
            raise ValueError('가상 매도 근거의 시간대를 확인해 주세요.')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            return paper_exits.sell(db,trade_id,quantity,observed_price,reason,fee_pct,tax_pct,slip_pct,
                                    stamp=observed_at.astimezone(KST).isoformat() if observed_at is not None else now(),request_key=request_key)

    def paper_exit_history(self, trade_id):
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM paper_exits WHERE trade_id=? ORDER BY id',(trade_id,))]

    def practice_sale_report(self, trade_id=None, page=1):
        from app.practice_details import sale_details
        if type(page) is not int or not 1<=page<=999999:
            return '페이지 번호를 확인해 주세요.'
        with self.connect() as db:
            db.execute('BEGIN')
            if trade_id is None:
                count=db.execute('SELECT COUNT(*) FROM paper_trades').fetchone()[0]
                pages=max(1,(count+4)//5)
                if page>pages:
                    return f'마지막 목록 페이지는 {pages}예요.'
                rows=db.execute('SELECT id,name,remaining_qty FROM paper_trades ORDER BY id DESC LIMIT 5 OFFSET ?',((page-1)*5,)).fetchall()
                return (f'[앱 안의 연습 매도 기록 · 실제 체결 아님]\n목록 {page}/{pages}페이지 · 전체 {count}개\n'+
                        ('\n'.join(f'{r["id"]}: {r["name"]} · 남은 {r["remaining_qty"]}주' for r in rows)
                         if rows else '연습 기록이 없어요.')+'\n사용법: /연습내역 기록번호\n예: /연습내역 1'+
                        (f'\n다음 목록: /연습내역 목록 {page+1}' if page<pages else ''))
            if type(trade_id) is not int or trade_id<=0 or type(page) is not int or not 1<=page<=999999:
                return '기록 번호와 페이지를 확인해 주세요.'
            trade=db.execute('SELECT * FROM paper_trades WHERE id=?',(trade_id,)).fetchone()
            if trade is None:
                return '이 모드의 연습 기록에 없는 번호예요.'
            count=db.execute('SELECT COUNT(*) FROM paper_exits WHERE trade_id=?',(trade_id,)).fetchone()[0]
            pages=max(1,count)
            if page>pages:
                return f'마지막 페이지는 {pages}예요.'
            sales=db.execute('SELECT * FROM paper_exits WHERE trade_id=? ORDER BY id LIMIT 1 OFFSET ?',
                             (trade_id,page-1)).fetchall()
            return (f'[연습 매도 내역 · {page}/{pages}페이지]\n'+sale_details(trade,sales,start=page)+
                    (f'\n다음: /연습내역 {trade_id} {page+1}' if page<pages else ''))

    def practice_daily_report(self, day, page=1):
        from app.practice_daily import summarize,render
        with self.connect() as db:
            return render(summarize(db,day),page=page)

    def open_trades(self):
        with self.connect() as db:
            return [dict(x) for x in db.execute('SELECT * FROM paper_trades WHERE closed_at IS NULL ORDER BY id DESC')]

    def trades(self, limit=1000):
        with self.connect() as db:
            return [dict(x) for x in db.execute('SELECT * FROM paper_trades ORDER BY id DESC LIMIT ?', (limit,))]

    def closed_trades(self):
        with self.connect() as db:
            return [dict(x) for x in db.execute('SELECT * FROM paper_trades WHERE closed_at IS NOT NULL ORDER BY id')]

    def condition_reviews(self, min_train=20):
        with self.connect() as db:
            trades = [dict(x) for x in db.execute('SELECT * FROM paper_trades ORDER BY id')]
        return review_conditions(trades, min_train)

    def stats(self):
        with self.connect() as db:
            rows=[dict(row) for row in db.execute('SELECT * FROM paper_trades')]
        closed = [t for t in rows if t['closed_at'] and outcome(t) in ('승','패','본전')]
        open_rows=[t for t in rows if not t['closed_at']]
        partial=[t['realized_pnl'] for t in open_rows
                 if type(t['realized_pnl']) in (int,float) and isfinite(t['realized_pnl'])]
        wins = [t for t in closed if t['pnl'] > 0]
        closed_pnl = fsum(t['pnl'] for t in closed)
        amounts=[t['pnl'] for t in closed]+partial
        gross_win = fsum(value for value in amounts if value>0)
        gross_loss = -fsum(value for value in amounts if value<0)
        total = fsum(amounts)
        closed_gain=fsum(t['pnl'] for t in closed if t['pnl']>0)
        closed_loss=-fsum(t['pnl'] for t in closed if t['pnl']<0)
        return {
            'count': len(closed), 'wins': len(wins), 'winrate': len(wins) / len(closed) * 100 if closed else 0,
            'pnl': total, 'gain': gross_win, 'loss': gross_loss,
            'profit_factor': closed_gain / closed_loss if closed_loss else (float('inf') if closed_gain else 0),
            'expectancy': closed_pnl / len(closed) if closed else 0,
            'open_count': len(open_rows), 'partial_pnl':fsum(partial),'closed_pnl':closed_pnl,
            'unknown_count':len(rows)-len(closed)-len(partial)
        }

    def proposals(self):
        with self.connect() as db:
            return [dict(x) for x in db.execute('SELECT * FROM proposals ORDER BY id DESC LIMIT 100')]

    def propose(self, title, rationale, patch,*,base_settings=None):
        context=None
        if base_settings is not None:
            from app.modes import current_mode
            base=settings_snapshot(base_settings)
            if (set(base)!=set(SNAPSHOT_KEYS) or base['data_mode'] not in ('DEMO','KIWOOM')
                    or base['kiwoom_env'] not in ('MOCK','REAL')):
                raise ValueError('개선안 생성 당시의 조건·모드를 확인해 주세요.')
            context=(current_mode(base).key,json.dumps(base,ensure_ascii=False,sort_keys=True,allow_nan=False))
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            # prevent identical unreviewed proposal duplication
            p = json.dumps(patch, ensure_ascii=False, sort_keys=True)
            duplicate=(db.execute('SELECT 1 FROM proposals p JOIN proposal_context c ON c.proposal_id=p.id WHERE p.patch_json=? AND p.state=? AND c.mode=? AND c.base_json=?',
                                 (p,'PENDING',*context)).fetchone() if context else
                       db.execute('SELECT 1 FROM proposals WHERE patch_json=? AND state=?',(p,'PENDING')).fetchone())
            if duplicate:
                return False
            cursor=db.execute('INSERT INTO proposals(created_at,title,rationale,patch_json,state) VALUES(?,?,?,?,?)',
                       (now(), title, rationale, p, 'PENDING'))
            if context:db.execute('INSERT INTO proposal_context VALUES(?,?,?)',(cursor.lastrowid,*context))
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
        for row in rows:
            row['outcome'] = outcome(row)
        columns = ['id','code','name','market','opened_at','closed_at','qty','buy_ref','buy_fill',
                   'buy_fee','stop_pct','take_pct','close_ref','close_fill','close_fee','close_tax',
                   'close_reason','pnl','pnl_pct','source','strat_json','entry_json','exit_settings_json',
                   'remaining_qty','realized_pnl','outcome']
        with open(filename, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=columns, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(rows)

    def export_paper_exits(self,filename):
        with self.connect() as db:
            rows=[dict(row) for row in db.execute('SELECT * FROM paper_exits ORDER BY id')]
        fields=['id','trade_id','sold_at','qty','reference','fill','sell_fee','sell_tax','allocated_cost','pnl','costs_json','reason']
        with open(filename,'w',newline='',encoding='utf-8-sig') as stream:
            writer=csv.DictWriter(stream,fieldnames=fields,extrasaction='ignore')
            writer.writeheader();writer.writerows(rows)

    def export_snapshots(self, filename):
        with self.connect() as db:
            rows = [dict(r) for r in db.execute('SELECT * FROM snapshots ORDER BY id DESC')]
        cols = ['id','scan_id','ts','code','name','market','price','change_pct','value_eok',
                'vol_ratio','cap_eok','score','passed','reason','source']
        with open(filename,'w',newline='', encoding='utf-8-sig') as f:
            w = csv.DictWriter(f,fieldnames=cols)
            w.writeheader()
            w.writerows(rows)
