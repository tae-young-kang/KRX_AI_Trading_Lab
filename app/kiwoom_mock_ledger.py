"""Durable MOCK fill evidence and explicitly supplied, scoped final costs.

No HTTP or execution. ka10076 observations NEVER become confirmed costs.
An external cost confirmation is bound to the exact fill set and evidence item.
"""
from dataclasses import asdict, dataclass
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import re

from app.order_engine import OrderRequest, connect
from app.kiwoom_mock_contract import ContractError
from app.kiwoom_mock_review import Execution, FeeObservation, Observations, order_number, stock_code


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def _amount(value, *, signed=False):
    if type(value) is not int or abs(value) > 10**15 or (not signed and value < 0):
        raise ContractError('수량·비용 범위를 확인하세요.')
    return value


def _day(value):
    if type(value) is not date:
        raise ContractError('체결 거래일이 필요합니다.')
    return value.isoformat()


@dataclass(frozen=True)
class OrderExecutionView:
    day: date
    order_no: str
    code: str
    side: str
    filled_qty: int
    filled_value: int
    commission: int | None
    tax: int | None
    net_cash_flow: int | None
    cost_source: str | None
    fill_state: str
    ready_for_orders: bool = False


class MockFillLedger:
    environment = 'MOCK'

    def __init__(self, path, *, account_tag):
        if not isinstance(account_tag, str) or not re.fullmatch(r'[a-f0-9]{64}', account_tag):
            raise ContractError('확인된 모의 계좌 구분값이 필요합니다.')
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._account_tag = account_tag
        with connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables - {'fill_meta', 'fill_events', 'fill_snapshots', 'fill_costs', 'cancel_review_events'}:
                raise ContractError('체결 장부는 별도 모의 기록 파일이어야 합니다.')
            db.execute('CREATE TABLE IF NOT EXISTS fill_meta (account_tag TEXT PRIMARY KEY)')
            prior = db.execute('SELECT account_tag FROM fill_meta').fetchone()
            if prior and prior[0] != account_tag:
                raise ContractError('다른 모의 계좌의 체결 장부입니다.')
            if not prior:
                db.execute('INSERT INTO fill_meta VALUES (?)', (account_tag,))
            db.execute('''CREATE TABLE IF NOT EXISTS fill_events (
                day TEXT, order_no TEXT, fill_no TEXT, payload TEXT NOT NULL,
                PRIMARY KEY(day,order_no,fill_no))''')
            db.execute('''CREATE TABLE IF NOT EXISTS fill_snapshots (
                day TEXT PRIMARY KEY, payload TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS fill_costs (
                day TEXT, order_no TEXT, fill_digest TEXT, commission INTEGER, tax INTEGER,
                source TEXT, evidence_hash TEXT, evidence_item TEXT,
                PRIMARY KEY(day,order_no,fill_digest), UNIQUE(evidence_hash,evidence_item))''')
            db.execute('''CREATE TABLE IF NOT EXISTS cancel_review_events (
                review_id TEXT PRIMARY KEY,root_key TEXT,day TEXT,checked_at TEXT,
                remaining_quantity INTEGER,filled_quantity INTEGER,filled_value INTEGER)''')

    def record_cancellation_review(self, root_key, review):
        from app.kiwoom_mock_cancel_review import CancellationReview
        if (type(review) is not CancellationReview or review.account_tag != self._account_tag
                or not isinstance(root_key, str) or not 1 <= len(root_key) <= 128
                or review.final_cancellation_proven or review.ready_for_orders):
            raise ContractError('읽기 전용 모의 취소 대조 기록이 필요합니다.')
        if not isinstance(review.review_id,str) or not re.fullmatch(r'[a-f0-9]{64}',review.review_id):
            raise ContractError('대조 근거 식별자가 필요합니다.')
        day = date.fromisoformat(review.day)
        moment = datetime.fromisoformat(review.checked_at)
        if moment.tzinfo is None or moment.date() != day:
            raise ContractError('대조 날짜·시간을 확인하세요.')
        for amount in (review.remaining_quantity,review.filled_quantity,review.filled_value):
            _amount(amount)
        values = (review.review_id,root_key,review.day,review.checked_at,review.remaining_quantity,
                  review.filled_quantity,review.filled_value)
        with connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT * FROM cancel_review_events WHERE review_id=?',(review.review_id,)).fetchone()
            if previous:
                if tuple(previous) != values:
                    raise ContractError('같은 대조 근거의 기록이 다릅니다. 덮어쓰지 않습니다.')
                return
            db.execute('INSERT INTO cancel_review_events VALUES (?,?,?,?,?,?,?)', values)

    def ingest(self, snapshot):
        if type(snapshot) is not Observations or snapshot.account_tag != self._account_tag:
            raise ContractError('모의 계좌가 일치하는 조회 기록이 필요합니다.')
        day = _day(snapshot.day)
        money_keys = ('entr', 'ord_alow_amt', '100stk_ord_alow_amt', 'd1_entra', 'd2_entra')
        if tuple(key for key, _ in snapshot.deposit) != money_keys:
            raise ContractError('예수금 조회 항목이 불완전합니다.')
        for _, value in snapshot.deposit:
            _amount(value, signed=True)
        seen_codes = set()
        for code, quantity, available in snapshot.holdings:
            if stock_code(code) != code or code in seen_codes:
                raise ContractError('보유 종목이 중복되거나 잘못됐습니다.')
            _amount(quantity)
            _amount(available)
            if available > quantity:
                raise ContractError('매도 가능 수량이 보유량보다 큽니다.')
            seen_codes.add(code)
        for fee in snapshot.fees:
            if (type(fee) is not FeeObservation or order_number(fee.order_no) != fee.order_no
                    or stock_code(fee.code) != fee.code):
                raise ContractError('비용 관측값 형식을 확인하세요.')
            _amount(fee.commission)
            _amount(fee.tax)
        seen_orders = set()
        for number, code, quantity, remaining in snapshot.unfilled:
            _amount(quantity)
            _amount(remaining)
            if (order_number(number) != number or stock_code(code) != code
                    or number in seen_orders or not 0 < remaining <= quantity):
                raise ContractError('미체결 기록이 불완전합니다.')
            seen_orders.add(number)
        incoming, order_shapes = {}, {}
        for item in snapshot.executions:
            if (type(item) is not Execution or item.side not in ('BUY', 'SELL')
                    or item.quantity <= 0 or item.price <= 0):
                raise ContractError('체결 항목을 확인하세요.')
            _amount(item.quantity)
            _amount(item.price)
            _amount(item.gross)
            if (order_number(item.order_no) != item.order_no or order_number(item.fill_no) != item.fill_no
                    or stock_code(item.code) != item.code
                    or (item.original_no is not None and order_number(item.original_no) != item.original_no)):
                raise ContractError('정규화한 체결 번호·종목코드가 필요합니다.')
            key = item.order_no, item.fill_no
            shape = item.code, item.side, item.original_no
            if item.order_no in order_shapes and order_shapes[item.order_no] != shape:
                raise ContractError('같은 주문번호의 종목·방향·원주문이 다릅니다.')
            order_shapes[item.order_no] = shape
            value = _json(asdict(item))
            if key in incoming and incoming[key] != value:
                raise ContractError('같은 체결 번호의 내용이 다릅니다.')
            incoming[key] = value
        payload = asdict(snapshot)
        payload['day'] = day
        # Account tag is stored once in metadata, never raw account/credentials.
        del payload['account_tag']
        serialized = _json(payload)
        with connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            previous = {(row['order_no'], row['fill_no']): row['payload'] for row in
                        db.execute('SELECT * FROM fill_events WHERE day=?', (day,))}
            if any(incoming.get(key) != value for key, value in previous.items()):
                raise ContractError('이전 체결이 누락되거나 변경됐습니다. 장부를 갱신하지 않습니다.')
            db.executemany('INSERT OR IGNORE INTO fill_events VALUES (?,?,?,?)',
                ((day, key[0], key[1], value) for key, value in incoming.items()))
            db.execute('INSERT OR REPLACE INTO fill_snapshots VALUES (?,?)', (day, serialized))
        return len(incoming) - len(previous)

    @staticmethod
    def _fills(db, day, order_no):
        rows = db.execute('SELECT payload FROM fill_events WHERE day=? AND order_no=? ORDER BY fill_no',
                          (day, order_no)).fetchall()
        items = [json.loads(row[0]) for row in rows]
        digest = hashlib.sha256(_json(items).encode()).hexdigest()
        return items, digest

    def confirm_costs(self, day, order_no, *, commission, tax, filled_quantity, gross,
                      source, evidence_hash, evidence_item):
        """Record caller-verified per-order totals, not an automatic verification.

        Production caller must independently check a MOCK broker statement.
        SYNTHETIC_TEST is explicitly labelled and never proves broker execution.
        No statement contents, private filenames, or arbitrary notes are stored.
        """
        day, order_no = _day(day), order_number(order_no)
        for value in (commission, tax, filled_quantity, gross):
            _amount(value)
        if (source not in ('BROKER_MOCK_STATEMENT', 'SYNTHETIC_TEST')
                or not isinstance(evidence_hash, str) or not re.fullmatch(r'[a-f0-9]{64}', evidence_hash)
                or not isinstance(evidence_item, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', evidence_item)):
            raise ContractError('주문별 비용 확인 근거가 필요합니다.')
        with connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            items, digest = self._fills(db, day, order_no)
            if (not items or sum(item['quantity'] for item in items) != filled_quantity
                    or sum(item['quantity'] * item['price'] for item in items) != gross):
                raise ContractError('비용 근거의 체결 수량·금액이 장부와 다릅니다.')
            values = (day, order_no, digest, commission, tax, source, evidence_hash, evidence_item)
            prior = db.execute('SELECT * FROM fill_costs WHERE day=? AND order_no=? AND fill_digest=?',
                               (day, order_no, digest)).fetchone()
            if prior:
                if tuple(prior) != values:
                    raise ContractError('이미 확인한 비용과 다릅니다. 기존 근거를 덮어쓰지 않습니다.')
                return
            if db.execute('SELECT 1 FROM fill_costs WHERE evidence_hash=? AND evidence_item=?',
                          (evidence_hash, evidence_item)).fetchone():
                raise ContractError('같은 근거 항목을 다른 체결에 중복 적용할 수 없습니다.')
            db.execute('INSERT INTO fill_costs VALUES (?,?,?,?,?,?,?,?)', values)

    def project_order(self, day, order_no, request):
        """Read-only bridge using the engine's OrderRequest, no broker mutation.

        Single broker order number only; amend chains require further mapping.
        Cash flow is trade consideration, NOT cash available or settled deposit.
        """
        day_text, number = _day(day), order_number(order_no)
        if type(request) is not OrderRequest:
            raise ContractError('주문 엔진 형식의 요청이 필요합니다.')
        request.validate()
        if request.venue != 'KRX':
            raise ContractError('KRX 모의 기록만 대조합니다.')
        with connect(self.path) as db:
            items, digest = self._fills(db, day_text, number)
            cost = db.execute('SELECT * FROM fill_costs WHERE day=? AND order_no=? AND fill_digest=?',
                              (day_text, number, digest)).fetchone()
        for item in items:
            if (item['code'] != request.code or item['side'] != request.side
                    or (request.side == 'BUY' and item['price'] > request.limit_price)
                    or (request.side == 'SELL' and item['price'] < request.limit_price)):
                raise ContractError('주문 내용·지정가와 체결 기록이 다릅니다.')
        quantity = sum(item['quantity'] for item in items)
        gross = sum(item['quantity'] * item['price'] for item in items)
        if quantity > request.quantity:
            raise ContractError('체결량이 주문량을 초과합니다.')
        commission, tax, net, source = None, None, None, None
        if cost:
            commission, tax, source = cost['commission'], cost['tax'], cost['source']
            net = (gross if request.side == 'SELL' else -gross) - commission - tax
        state = ('NO_FILL_EVIDENCE' if quantity == 0 else
                 'PARTIAL_FILL_OBSERVED' if quantity < request.quantity else 'FULL_FILL_OBSERVED')
        return OrderExecutionView(day, number, request.code, request.side, quantity, gross,
                                  commission, tax, net, source, state)
