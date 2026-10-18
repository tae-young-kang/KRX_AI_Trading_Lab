"""Durable MOCK dispatch receipts, not an executable account/order engine.

UNKNOWN has no automatic clearing/retry path. An ACK never claims a fill or a
completed cancellation. Network integration in the UI remains disabled.
"""
import json
from pathlib import Path

from app.order_engine import connect
from app.kiwoom_mock_contract import (
    ContractError, RequestRefused, UncertainResponse, acknowledgement,
)
from app.kiwoom_mock_transport import MockTransport, validated_plan
from app.runtime_lock import ReceiverLock
from app.kiwoom_mock_review import order_number


class MockDispatchJournal:
    environment = 'MOCK'

    def __init__(self, path, transport):
        if type(transport) is not MockTransport or transport.environment != 'MOCK':
            raise ContractError('모의 전용 통신부가 필요합니다.')
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.transport = transport
        self._profile = transport.profile_id
        self._lock_path = self.path.with_suffix(self.path.suffix + '.dispatch.lock')
        with ReceiverLock(self._lock_path), connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables - {'mock_identity', 'mock_dispatch', 'mock_account'}:
                raise ContractError('가상 연습·다른 기능과 주문 기록 파일을 공유할 수 없습니다.')
            db.execute('CREATE TABLE IF NOT EXISTS mock_identity (profile TEXT PRIMARY KEY)')
            row = db.execute('SELECT profile FROM mock_identity').fetchone()
            if row and row['profile'] != self._profile:
                raise ContractError('다른 모의 프로필의 주문 기록입니다.')
            if not row:
                db.execute('INSERT INTO mock_identity VALUES (?)', (self._profile,))
            # Preserve undated legacy receipts without inferring a trading day.
            if 'mock_dispatch' in tables and 'trade_day' not in {
                    row['name'] for row in db.execute('PRAGMA table_info(mock_dispatch)')}:
                db.execute('ALTER TABLE mock_dispatch RENAME TO mock_dispatch_legacy')
            db.execute('''CREATE TABLE IF NOT EXISTS mock_dispatch (
                request_key TEXT PRIMARY KEY, api_id TEXT NOT NULL, payload TEXT NOT NULL,
                parent_key TEXT, root_key TEXT NOT NULL, state TEXT NOT NULL,
                order_no TEXT, base_order_no TEXT, trade_day TEXT,
                UNIQUE(trade_day,order_no))''')
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='mock_dispatch_legacy'").fetchone():
                db.execute('''INSERT INTO mock_dispatch SELECT request_key,api_id,payload,
                    parent_key,root_key,state,order_no,base_order_no,NULL FROM mock_dispatch_legacy''')
                db.execute('DROP TABLE mock_dispatch_legacy')
            db.execute('CREATE TABLE IF NOT EXISTS mock_account (account_tag TEXT PRIMARY KEY)')
            # Lock spans each entire dispatch, so nobody can still own SENDING.
            db.execute("UPDATE mock_dispatch SET state='UNKNOWN' WHERE state='SENDING'")

    def bind_account(self, account_tag):
        """Caller supplies independently verified account tag BEFORE dispatch.

        This is local evidence binding, not verification of a broker login.
        Old unbound receipts cannot be assigned to an account retrospectively.
        """
        import re
        if not isinstance(account_tag, str) or not re.fullmatch(r'[a-f0-9]{64}', account_tag):
            raise ContractError('확인된 모의 계좌 구분값이 필요합니다.')
        with ReceiverLock(self._lock_path), connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT account_tag FROM mock_account').fetchone()
            if prior:
                if prior[0] != account_tag:
                    raise ContractError('다른 모의 계좌의 접수 기록입니다.')
            elif db.execute('SELECT 1 FROM mock_dispatch').fetchone():
                raise ContractError('기존 미연결 기록의 계좌를 추정할 수 없습니다.')
            else:
                db.execute('INSERT INTO mock_account VALUES (?)', (account_tag,))

    def read_chain(self, root_key):
        with ReceiverLock(self._lock_path), connect(self.path) as db:
            db.execute('BEGIN')
            bound = db.execute('SELECT account_tag FROM mock_account').fetchone()
            if not bound:
                raise ContractError('접수 기록의 모의 계좌 연결이 필요합니다.')
            rows = db.execute('SELECT * FROM mock_dispatch WHERE root_key=?', (root_key,)).fetchall()
            return bound[0], [dict(row) for row in rows]

    def get(self, key):
        with connect(self.path) as db:
            row = db.execute('SELECT * FROM mock_dispatch WHERE request_key=?', (key,)).fetchone()
        return dict(row) if row else None

    def dispatch(self, key, plan, *, parent_key=None):
        plan = validated_plan(plan)
        if plan.api_id not in ('kt10000', 'kt10001', 'kt10002', 'kt10003'):
            raise ContractError('주문 요청만 기록할 수 있습니다.')
        if not isinstance(key, str) or not 1 <= len(key) <= 128:
            raise ContractError('고유 요청 번호가 필요합니다.')
        payload = json.dumps(plan.body(), sort_keys=True, separators=(',', ':'))
        with ReceiverLock(self._lock_path):
            if self.transport.profile_id != self._profile or self.transport.environment != 'MOCK':
                raise ContractError('모의 연결 정보가 변경됐습니다.')
            with connect(self.path) as db:
                db.execute('BEGIN IMMEDIATE')
                existing = db.execute('SELECT * FROM mock_dispatch WHERE request_key=?', (key,)).fetchone()
                if existing:
                    if (existing['api_id'], existing['payload'], existing['parent_key']) != (plan.api_id, payload, parent_key):
                        raise ContractError('같은 요청 번호의 주문 내용이 다릅니다.')
                    return dict(existing)
                if db.execute("SELECT 1 FROM mock_dispatch WHERE state IN ('SENDING','UNKNOWN')").fetchone():
                    raise ContractError('미확인 주문이 있습니다. 새 요청을 차단합니다.')
                if db.execute('SELECT 1 FROM mock_dispatch WHERE trade_day IS NULL').fetchone():
                    raise ContractError('거래일 미확인 과거 기록이 있습니다. 새 요청을 차단합니다.')
                trade_day = self.transport.current_day.isoformat()
                generation = self.transport.auth_generation
                root = key
                if plan.api_id in ('kt10002', 'kt10003'):
                    parent = db.execute('SELECT * FROM mock_dispatch WHERE request_key=?', (parent_key,)).fetchone()
                    if (not parent or parent['state'] != 'ACKNOWLEDGED' or parent['api_id'] == 'kt10003'
                            or parent['trade_day'] != trade_day
                            or order_number(parent['order_no']) != order_number(plan.body()['orig_ord_no'])
                            or json.loads(parent['payload'])['stk_cd'] != plan.body()['stk_cd']):
                        raise ContractError('원주문 기록과 정정·취소 요청이 일치하지 않습니다.')
                    if db.execute("SELECT 1 FROM mock_dispatch WHERE parent_key=? AND state!='REJECTED'",
                                  (parent_key,)).fetchone():
                        raise ContractError('이미 후속 요청이 있습니다. 최신 주문번호를 확인하세요.')
                    root = parent['root_key']
                elif parent_key is not None:
                    raise ContractError('신규 주문에는 원주문을 지정할 수 없습니다.')
                self.transport.ready()  # Authentication failures precede durable sending state.
                db.execute('INSERT INTO mock_dispatch VALUES (?,?,?,?,?,?,NULL,NULL,?)',
                    (key, plan.api_id, payload, parent_key, root, 'SENDING', trade_day))
            try:
                if (self.transport.current_day.isoformat() != trade_day or self.transport.auth_generation != generation
                        or self.transport.profile_id != self._profile or self.transport.environment != 'MOCK'):
                    raise UncertainResponse('전송 전 날짜·인증이 변경됐습니다.')
                response, _ = self.transport.exchange(plan)
                ack = acknowledgement(plan.api_id, response)
                if (self.transport.current_day.isoformat() != trade_day or self.transport.auth_generation != generation
                        or self.transport.profile_id != self._profile or self.transport.environment != 'MOCK'):
                    raise UncertainResponse('전송 도중 날짜·인증이 변경됐습니다.')
                with connect(self.path) as db:
                    if parent_key is not None:
                        root_order = db.execute('SELECT order_no FROM mock_dispatch WHERE request_key=?', (root,)).fetchone()
                        if order_number(ack.base_order_no) != order_number(root_order['order_no']):
                            raise UncertainResponse('모주문번호가 불일치합니다.')
                    db.execute("UPDATE mock_dispatch SET state='ACKNOWLEDGED', order_no=?, base_order_no=? WHERE request_key=?",
                               (order_number(ack.order_no), order_number(ack.base_order_no) if ack.base_order_no else None, key))
            except RequestRefused:
                self._state(key, 'REJECTED')
            except Exception:
                self._state(key, 'UNKNOWN')
            # BaseException (e.g. abrupt process exit) leaves SENDING; reopen
            # under the OS lock marks UNKNOWN without sending anything again.
            return self.get(key)

    def _state(self, key, state):
        with connect(self.path) as db:
            db.execute('UPDATE mock_dispatch SET state=? WHERE request_key=?', (state, key))
