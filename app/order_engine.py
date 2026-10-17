"""Durable OFFLINE order lifecycle. No HTTP, credentials, or live adapter.

Only a SIMULATED broker is accepted. Response uncertainty blocks new mutations;
reconciliation looks up the existing client reference and NEVER resubmits it.
"""
import json
import sqlite3
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

from app.db import ClosingConnection
from app.trading_clock import KST


class OrderError(ValueError):
    pass


class OrderRejected(OrderError):
    """Definite broker rejection, unlike timeout/unknown delivery."""


def positive_int(value):
    return type(value) is int and value > 0


@dataclass(frozen=True)
class OrderRequest:
    code: str
    side: str
    quantity: int
    limit_price: int
    venue: str = 'KRX'

    def validate(self):
        if (not isinstance(self.code, str) or len(self.code) != 6 or
                not self.code.isascii() or not self.code.isalnum()):
            raise OrderError('6자리 종목코드가 필요합니다.')
        if self.side not in ('BUY', 'SELL') or self.venue not in ('KRX', 'NXT'):
            raise OrderError('매수/매도와 거래시장을 확인하세요.')
        if not positive_int(self.quantity) or not positive_int(self.limit_price):
            raise OrderError('수량과 지정가는 양의 정수여야 합니다.')


@dataclass(frozen=True)
class BrokerOrder:
    client_ref: str
    broker_id: str
    request: OrderRequest
    filled_qty: int = 0
    filled_value: int = 0
    state: str = 'OPEN'


def connect(path):
    db = sqlite3.connect(str(path), timeout=20, factory=ClosingConnection)
    db.row_factory = sqlite3.Row
    return db


class OrderEngine:
    ACTIVE = ('SENDING', 'OPEN', 'PARTIAL', 'UNKNOWN')

    def __init__(self, path, broker, calendar, clock=None):
        if getattr(broker, 'environment', None) != 'SIMULATED':
            raise OrderError('이 엔진은 오프라인 가짜 서버만 연결할 수 있습니다.')
        self.path = Path(path)
        if self.path.resolve() == Path(broker.path).resolve():
            raise OrderError('주문 기록과 가짜 서버 DB는 서로 다른 파일이어야 합니다.')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.broker, self.calendar = broker, calendar
        self.clock = clock or (lambda: datetime.now(KST))
        with connect(self.path) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS engine_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS orders(
                    id TEXT PRIMARY KEY,broker_id TEXT,code TEXT,side TEXT,venue TEXT,
                    quantity INTEGER,limit_price INTEGER,filled_qty INTEGER NOT NULL DEFAULT 0,
                    filled_value INTEGER NOT NULL DEFAULT 0,state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS actions(
                    key TEXT PRIMARY KEY,kind TEXT,order_id TEXT,payload TEXT,state TEXT,
                    before_state TEXT);
                CREATE TABLE IF NOT EXISTS positions(code TEXT PRIMARY KEY,quantity INTEGER);
                CREATE TABLE IF NOT EXISTS events(
                    id INTEGER PRIMARY KEY,order_id TEXT,kind TEXT,state TEXT,at TEXT);
            ''')
            if not db.execute("SELECT 1 FROM engine_meta WHERE key='cash'").fetchone():
                account = broker.account()
                db.execute("INSERT INTO engine_meta VALUES('cash',?)", (str(account['cash']),))
                db.execute("INSERT INTO engine_meta VALUES('broker_identity',?)", (broker.identity,))
                db.executemany('INSERT INTO positions VALUES(?,?)', account['positions'].items())
            identity = db.execute("SELECT value FROM engine_meta WHERE key='broker_identity'").fetchone()[0]
            if identity != broker.identity:
                raise OrderError('다른 가짜 계좌의 기록과 연결할 수 없습니다.')
        # Recovery is explicit: do not change an in-flight action just by opening
        # another engine instance. Only the stopped process owner may call recover.

    def get(self, order_id):
        with connect(self.path) as db:
            row = db.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone()
            if not row:
                raise OrderError('주문 기록이 없습니다.')
            return dict(row)

    def account(self):
        with connect(self.path) as db:
            return {'cash': int(db.execute("SELECT value FROM engine_meta WHERE key='cash'").fetchone()[0]),
                    'positions': {r['code']: r['quantity'] for r in db.execute('SELECT * FROM positions WHERE quantity>0')}}

    def recover(self):
        """Call only after the previous worker has stopped, under one owner."""
        with connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE orders SET state='UNKNOWN' WHERE id IN (SELECT order_id FROM actions WHERE state='SENDING')")
            db.execute("UPDATE actions SET state='UNKNOWN' WHERE state='SENDING'")

    def _reserve_check(self, db, request, filled=0, exclude=''):
        rows = db.execute("SELECT * FROM orders WHERE state IN ('SENDING','OPEN','PARTIAL','UNKNOWN') AND id!=?", (exclude,)).fetchall()
        remaining = request.quantity - filled
        if request.side == 'BUY':
            cash = int(db.execute("SELECT value FROM engine_meta WHERE key='cash'").fetchone()[0])
            reserved = sum((r['quantity']-r['filled_qty'])*r['limit_price'] for r in rows if r['side']=='BUY')
            if remaining * request.limit_price > cash - reserved:
                raise OrderError('예약한 주문을 제외하면 연습 잔액이 부족합니다.')
        else:
            row = db.execute('SELECT quantity FROM positions WHERE code=?', (request.code,)).fetchone()
            held = row[0] if row else 0
            reserved = sum(r['quantity']-r['filled_qty'] for r in rows if r['side']=='SELL' and r['code']==request.code)
            if remaining > held - reserved:
                raise OrderError('이미 매도 예약한 수량을 제외하면 보유 수량이 부족합니다.')

    def submit(self, key, request):
        request.validate()
        return self._act(key, 'SUBMIT', request=request)

    def amend(self, key, order_id, quantity, limit_price):
        return self._act(key, 'AMEND', order_id, quantity=quantity, limit_price=limit_price)

    def cancel(self, key, order_id):
        return self._act(key, 'CANCEL', order_id)

    def _act(self, key, kind, order_id='', request=None, **changes):
        if not isinstance(key, str) or not 1 <= len(key) <= 128:
            raise OrderError('재전송을 구분할 요청 번호가 필요합니다.')
        payload = json.dumps({'order_id': order_id, 'request': asdict(request) if request else None,
                              'changes': changes}, sort_keys=True)
        with connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM actions WHERE key=?', (key,)).fetchone()
            if old:
                if old['kind'] != kind or old['payload'] != payload:
                    raise OrderError('같은 요청 번호를 다른 주문에 사용할 수 없습니다.')
                return self.get(old['order_id'])
            if db.execute("SELECT 1 FROM actions WHERE state IN ('SENDING','UNKNOWN')").fetchone():
                raise OrderError('결과가 불명확한 주문부터 확인해야 합니다. 재전송하지 않습니다.')
            if db.execute("SELECT 1 FROM orders WHERE state='UNKNOWN'").fetchone():
                raise OrderError('체결 결과가 불명확한 주문부터 확인해야 합니다.')
            before = ''
            if kind != 'SUBMIT':
                row = db.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone()
                if not row or row['state'] not in ('OPEN', 'PARTIAL'):
                    raise OrderError('접수 중인 미체결 주문만 정정·취소할 수 있습니다.')
                before = row['state']
                request = OrderRequest(row['code'], row['side'], changes.get('quantity', row['quantity']),
                                       changes.get('limit_price', row['limit_price']), row['venue'])
                request.validate()
                if kind == 'AMEND' and request.quantity <= row['filled_qty']:
                    raise OrderError('정정 총수량은 체결 수량보다 커야 합니다. 남은 주문은 취소하세요.')
            if not self.calendar.is_open(self.clock(), request.venue):
                raise OrderError('확인된 거래일·거래시간이 아닙니다. 다음 장에 자동 예약하지 않습니다.')
            if kind != 'CANCEL':
                self._reserve_check(db, request, row['filled_qty'] if kind != 'SUBMIT' else 0, order_id)
            if kind == 'SUBMIT':
                order_id = uuid.uuid4().hex
                db.execute('INSERT INTO orders(id,code,side,venue,quantity,limit_price,state) VALUES(?,?,?,?,?,?,?)',
                           (order_id, request.code, request.side, request.venue, request.quantity, request.limit_price, 'SENDING'))
            else:
                db.execute("UPDATE orders SET state='SENDING' WHERE id=?", (order_id,))
            db.execute('INSERT INTO actions VALUES(?,?,?,?,?,?)', (key, kind, order_id, payload, 'SENDING', before))
        # Committed journal BEFORE the side effect; do not hold a transaction
        # over the broker call and do not retry side-effecting calls.
        try:
            if kind == 'SUBMIT':
                snapshot = self.broker.submit(order_id, request)
            elif kind == 'AMEND':
                snapshot = self.broker.amend(order_id, request.quantity, request.limit_price)
            else:
                snapshot = self.broker.cancel(order_id)
            self._apply(order_id, snapshot, key, request)
        except OrderRejected:
            with connect(self.path) as db:
                db.execute("UPDATE actions SET state='REJECTED' WHERE key=?", (key,))
                db.execute('UPDATE orders SET state=? WHERE id=?', (before or 'REJECTED', order_id))
                db.execute('INSERT INTO events(order_id,kind,state,at) VALUES(?,?,?,?)',
                           (order_id, kind, 'REJECTED', self.clock().isoformat()))
        except Exception:
            self._unknown(order_id, key)
        return self.get(order_id)

    def _unknown(self, order_id, key=None):
        with connect(self.path) as db:
            db.execute("UPDATE orders SET state='UNKNOWN' WHERE id=?", (order_id,))
            if key:
                db.execute("UPDATE actions SET state='UNKNOWN' WHERE key=?", (key,))
            db.execute('INSERT INTO events(order_id,kind,state,at) VALUES(?,?,?,?)',
                       (order_id, 'RECONCILE_NEEDED', 'UNKNOWN', self.clock().isoformat()))

    def reconcile(self, order_id):
        order = self.get(order_id)
        if order['state'] == 'REJECTED':
            return order
        with connect(self.path) as db:
            action = db.execute("SELECT * FROM actions WHERE order_id=? AND state IN ('SENDING','UNKNOWN')", (order_id,)).fetchone()
        expected = OrderRequest(order['code'], order['side'], order['quantity'], order['limit_price'], order['venue'])
        if action and action['kind'] == 'AMEND':
            changes = json.loads(action['payload'])['changes']
            expected = OrderRequest(order['code'], order['side'], changes['quantity'], changes['limit_price'], order['venue'])
        try:
            snapshot = self.broker.lookup(order_id)
            if snapshot is None:
                # No match does not prove an uncertain request was never sent.
                raise OrderError('서버에서 주문을 확인하지 못했습니다.')
            self._apply(order_id, snapshot, action['key'] if action else None, expected)
        except Exception:
            self._unknown(order_id, action['key'] if action else None)
        return self.get(order_id)

    def _apply(self, order_id, snapshot, key, expected):
        with connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone()
            if (not isinstance(snapshot, BrokerOrder) or snapshot.client_ref != order_id or
                    not snapshot.broker_id or snapshot.request != expected or
                    (old['broker_id'] and old['broker_id'] != snapshot.broker_id)):
                raise OrderError('요청과 다른 주문 응답입니다.')
            q, value, state = snapshot.filled_qty, snapshot.filled_value, snapshot.state
            if old['state'] in ('FILLED', 'CANCELED', 'REJECTED') and (
                    state != old['state'] or q != old['filled_qty'] or value != old['filled_value']):
                raise OrderError('종료된 주문의 상태가 과거로 돌아갔습니다.')
            if key:
                action = db.execute('SELECT kind FROM actions WHERE key=?', (key,)).fetchone()
                if action['kind'] == 'CANCEL' and state not in ('CANCELED', 'FILLED'):
                    raise OrderError('취소 완료를 확인하지 못했습니다.')
            if (type(q) is not int or type(value) is not int or
                    not old['filled_qty'] <= q <= expected.quantity or value < old['filled_value'] or
                    state not in ('OPEN', 'PARTIAL', 'FILLED', 'CANCELED') or
                    (state == 'OPEN' and q != 0) or
                    (state == 'PARTIAL' and not 0 < q < expected.quantity) or
                    (state == 'FILLED' and q != expected.quantity)):
                raise OrderError('체결 응답의 수량·상태가 잘못되었습니다.')
            dq, dv = q-old['filled_qty'], value-old['filled_value']
            if (dq == 0 and dv != 0) or (dq > 0 and dv <= 0):
                raise OrderError('체결 수량과 금액이 맞지 않습니다.')
            if dq and ((expected.side == 'BUY' and dv > dq*expected.limit_price) or
                       (expected.side == 'SELL' and dv < dq*expected.limit_price)):
                raise OrderError('지정가 밖의 체결입니다.')
            cash = int(db.execute("SELECT value FROM engine_meta WHERE key='cash'").fetchone()[0])
            pos = db.execute('SELECT quantity FROM positions WHERE code=?', (expected.code,)).fetchone()
            held = pos[0] if pos else 0
            sign = 1 if expected.side == 'BUY' else -1
            cash -= sign*dv
            held += sign*dq
            if cash < 0 or held < 0:
                raise OrderError('체결 후 잔액·수량이 음수입니다.')
            db.execute("UPDATE engine_meta SET value=? WHERE key='cash'", (str(cash),))
            db.execute('INSERT INTO positions VALUES(?,?) ON CONFLICT(code) DO UPDATE SET quantity=excluded.quantity', (expected.code, held))
            db.execute('UPDATE orders SET broker_id=?,quantity=?,limit_price=?,filled_qty=?,filled_value=?,state=? WHERE id=?',
                       (snapshot.broker_id, expected.quantity, expected.limit_price, q, value, state, order_id))
            if key:
                db.execute("UPDATE actions SET state='DONE' WHERE key=?", (key,))
            db.execute('INSERT INTO events(order_id,kind,state,at) VALUES(?,?,?,?)',
                       (order_id, 'SNAPSHOT', state, self.clock().isoformat()))
