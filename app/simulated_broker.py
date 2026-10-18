"""Persistent fake exchange for offline tests. No sockets or account secrets."""
import json
import uuid
from dataclasses import asdict
from pathlib import Path

from app.order_engine import BrokerOrder, OrderRequest, OrderRejected, connect, positive_int


class SimulatedBroker:
    environment = 'SIMULATED'

    def __init__(self, path, initial_cash=1_000_000):
        if type(initial_cash) is not int or initial_cash < 0:
            raise ValueError('연습 시작 금액을 확인하세요.')
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.calls = []
        # Test hook: a timeout AFTER the fake exchange accepted the mutation.
        self.lose_next_response = False
        with connect(self.path) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE IF NOT EXISTS positions(code TEXT PRIMARY KEY,quantity INTEGER);
                CREATE TABLE IF NOT EXISTS orders(
                    client_ref TEXT PRIMARY KEY,broker_id TEXT UNIQUE,request TEXT,
                    filled_qty INTEGER,filled_value INTEGER,state TEXT);
            ''')
            db.execute("INSERT OR IGNORE INTO meta VALUES('identity',?)", (uuid.uuid4().hex,))
            db.execute("INSERT OR IGNORE INTO meta VALUES('cash',?)", (str(initial_cash),))
            self.identity = db.execute("SELECT value FROM meta WHERE key='identity'").fetchone()[0]

    @staticmethod
    def _snapshot(row):
        return BrokerOrder(row['client_ref'], row['broker_id'], OrderRequest(**json.loads(row['request'])),
                           row['filled_qty'], row['filled_value'], row['state'])

    def lookup(self, client_ref):
        with connect(self.path) as db:
            row = db.execute('SELECT * FROM orders WHERE client_ref=?', (client_ref,)).fetchone()
            return self._snapshot(row) if row else None

    def account(self):
        with connect(self.path) as db:
            return {'cash': int(db.execute("SELECT value FROM meta WHERE key='cash'").fetchone()[0]),
                    'positions': {r['code']: r['quantity'] for r in db.execute('SELECT * FROM positions WHERE quantity>0')}}

    def open_orders(self):
        """Read-only inventory; detect orders absent from the engine journal."""
        with connect(self.path) as db:
            return [self._snapshot(r) for r in db.execute("SELECT * FROM orders WHERE state IN ('OPEN','PARTIAL')")]

    def _reply(self, client_ref):
        if self.lose_next_response:
            self.lose_next_response = False
            raise TimeoutError('simulated lost response')
        return self.lookup(client_ref)

    def submit(self, client_ref, request):
        request.validate()
        self.calls.append('submit')
        with connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM orders WHERE client_ref=?', (client_ref,)).fetchone()
            if old:
                if self._snapshot(old).request != request:
                    raise OrderRejected('같은 번호의 다른 주문입니다.')
            else:
                db.execute('INSERT INTO orders VALUES(?,?,?,?,?,?)',
                           (client_ref, 'SIM-'+uuid.uuid4().hex, json.dumps(asdict(request)), 0, 0, 'OPEN'))
        return self._reply(client_ref)

    def amend(self, client_ref, quantity, limit_price):
        self.calls.append('amend')
        with connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM orders WHERE client_ref=?', (client_ref,)).fetchone()
            if not row or row['state'] not in ('OPEN', 'PARTIAL') or quantity <= row['filled_qty']:
                raise OrderRejected('정정 가능한 주문이 아닙니다.')
            previous = self._snapshot(row).request
            request = OrderRequest(previous.code, previous.side, quantity, limit_price, previous.venue)
            request.validate()
            db.execute('UPDATE orders SET request=? WHERE client_ref=?', (json.dumps(asdict(request)), client_ref))
        return self._reply(client_ref)

    def cancel(self, client_ref):
        self.calls.append('cancel')
        with connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT state FROM orders WHERE client_ref=?', (client_ref,)).fetchone()
            if not row or row['state'] not in ('OPEN', 'PARTIAL'):
                raise OrderRejected('취소 가능한 주문이 아닙니다.')
            db.execute("UPDATE orders SET state='CANCELED' WHERE client_ref=?", (client_ref,))
        return self._reply(client_ref)

    def fill(self, client_ref, quantity, price):
        """Explicit test event; never invent a fill from OHLC or last price."""
        if not positive_int(quantity) or not positive_int(price):
            raise ValueError('체결 수량과 가격을 확인하세요.')
        with connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM orders WHERE client_ref=?', (client_ref,)).fetchone()
            if not row or row['state'] not in ('OPEN', 'PARTIAL'):
                raise OrderRejected('체결 가능한 주문이 아닙니다.')
            request = self._snapshot(row).request
            if quantity > request.quantity-row['filled_qty']:
                raise OrderRejected('미체결 수량보다 큽니다.')
            if ((request.side == 'BUY' and price > request.limit_price) or
                    (request.side == 'SELL' and price < request.limit_price)):
                raise OrderRejected('지정가 밖의 체결입니다.')
            cash = int(db.execute("SELECT value FROM meta WHERE key='cash'").fetchone()[0])
            position = db.execute('SELECT quantity FROM positions WHERE code=?', (request.code,)).fetchone()
            held = position[0] if position else 0
            sign = 1 if request.side == 'BUY' else -1
            cash -= sign*quantity*price
            held += sign*quantity
            if cash < 0 or held < 0:
                raise OrderRejected('잔액 또는 보유 수량이 부족합니다.')
            db.execute("UPDATE meta SET value=? WHERE key='cash'", (str(cash),))
            db.execute('INSERT INTO positions VALUES(?,?) ON CONFLICT(code) DO UPDATE SET quantity=excluded.quantity', (request.code, held))
            filled = row['filled_qty']+quantity
            db.execute('UPDATE orders SET filled_qty=?,filled_value=?,state=? WHERE client_ref=?',
                       (filled, row['filled_value']+quantity*price,
                        'FILLED' if filled==request.quantity else 'PARTIAL', client_ref))
        return self.lookup(client_ref)
