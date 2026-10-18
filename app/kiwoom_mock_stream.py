"""MOCK-only order notices. Explicit start, no orders or automatic reconnect.

Not connected to GUI/background startup. Test with injected fake connectors.
Network receipt is an observation; missing events never imply cancellation.
"""
from datetime import datetime
import hashlib
import json
import logging
import time
import uuid

from app.kiwoom_mock_contract import ContractError, UncertainResponse
from app.kiwoom_mock_review import MockAccountReviewer
from app.kiwoom_mock_status import OrderNotice, order_notice
from app.kiwoom_mock_transport import MockTransport
from app.trading_clock import KST


MOCK_SOCKET = 'wss://mockapi.kiwoom.com:10000/api/dostk/websocket'


def _connect():
    # Deferred import: constructing/importing the receiver never opens a socket.
    from websockets.sync.client import connect
    logger = logging.Logger('krx_mock_socket_private', level=logging.CRITICAL+1)
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    # The synchronous 15.x connector makes one handshake, doesn't follow HTTP
    # redirects, and never iterates an automatic reconnect helper.
    return connect(MOCK_SOCKET, proxy=None, compression=None, open_timeout=5,
                   close_timeout=1, ping_interval=20, ping_timeout=20,
                   max_size=65536, max_queue=16, logger=logger)


class MockOrderStream:
    environment = 'MOCK'
    ready_for_orders = False

    def __init__(self, transport, *, connector=None, monotonic=None):
        if type(transport) is not MockTransport or transport.environment != 'MOCK':
            raise ContractError('모의 전용 알림 연결이 필요합니다.')
        self.transport = transport
        self._connector = connector or _connect
        self._monotonic = monotonic or time.monotonic
        self._socket = None
        self._expected_account = None
        self._account_tag = None
        self._generation = None
        self._profile = None
        self._started_at = None
        self._handshake_at = None
        self._seen = set()
        self.session = None
        self.state = 'NEW'

    @property
    def status(self):
        return {'NEW':'알림 연결 전 · 주문 불가', 'LOGIN_WAIT':'모의 연결 확인 중 · 주문 불가',
                'REGISTER_WAIT':'주문 알림 준비 중 · 주문 불가',
                'RECEIVING':'모의 주문 알림 받는 중 · 주문 불가',
                'NEEDS_RECHECK':'연결 끊김 · 다시 확인 필요', 'CLOSED':'알림 받기 중지'}.get(self.state,'다시 확인 필요')

    def _now(self):
        now = self.transport._clock()
        if not isinstance(now,datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ContractError('알림 연결 시간을 확인하세요.')
        return now.astimezone(KST)

    def open(self, *, expected_account):
        if self.state != 'NEW':
            raise ContractError('같은 연결을 재사용하지 않습니다. 계좌 확인 후 새 연결을 만들어 주세요.')
        try:
            self.transport.ready()
            self._generation, self._profile = self.transport.auth_generation, self.transport.profile_id
            self._started_at = self._now()
            # Verify expected account before disclosing a token on the socket.
            self._account_tag = MockAccountReviewer(self.transport)._identity(expected_account)
            self._expected_account = expected_account
            self._guard()
            self._socket = self._connector()
            self._guard()
            self.session = uuid.uuid4().hex
            self._send({'trnm':'LOGIN','token':self.transport._token})
            self.state = 'LOGIN_WAIT'
            self._handshake_at = self._monotonic()
        except Exception:
            self._fail()
            raise UncertainResponse('모의 알림 연결을 확인할 수 없어요. 자동으로 다시 연결하지 않아요.') from None

    def _guard(self):
        self.transport.ready()
        if (self.transport.environment != 'MOCK' or self.transport.profile_id != self._profile
                or self.transport.auth_generation != self._generation
                or self._now().date() != self._started_at.date()):
            raise UncertainResponse('날짜·인증·모의 연결이 바뀌었습니다.')
        if self._socket is not None and (getattr(self._socket,'close_code',None) is not None
                                        or getattr(self._socket,'closed',False) is True):
            raise UncertainResponse('알림 연결이 닫혔습니다.')

    def _send(self, message):
        if message.get('trnm') not in ('LOGIN','REG','PING'):
            raise ContractError('허용하지 않는 알림 요청입니다.')
        self._socket.send(json.dumps(message, ensure_ascii=False))

    def read_once(self):
        if self.state not in ('LOGIN_WAIT','REGISTER_WAIT','RECEIVING'):
            raise ContractError('알림 연결을 다시 확인해 주세요.')
        try:
            self._guard()
            if self.state != 'RECEIVING' and self._monotonic()-self._handshake_at > 10:
                raise UncertainResponse('연결 확인 시간이 지났습니다.')
            try:
                raw = self._socket.recv(timeout=1)
            except TimeoutError:
                self._guard()
                return []  # Quiet account isn't evidence of a fill/cancellation.
            self._guard()
            if self.state != 'RECEIVING' and self._monotonic()-self._handshake_at > 10:
                raise UncertainResponse('연결 확인 시간이 지났습니다.')
            if not isinstance(raw,str) or len(raw.encode('utf-8')) > 65536:
                raise UncertainResponse('알림 크기·형식이 올바르지 않습니다.')
            frame = json.loads(raw)
            if not isinstance(frame,dict):
                raise UncertainResponse('알림 형식이 올바르지 않습니다.')
            kind = frame.get('trnm')
            if kind == 'PING':
                self._send(frame)  # Official JSON heartbeat: echo without logging.
                return []
            if kind in ('LOGIN','REG'):
                expected = 'LOGIN_WAIT' if kind == 'LOGIN' else 'REGISTER_WAIT'
                if (self.state != expected or type(frame.get('return_code')) is not int
                        or frame['return_code'] != 0 or (kind == 'REG' and frame.get('grp_no','1') != '1')):
                    raise UncertainResponse('알림 등록을 확인할 수 없습니다.')
                if kind == 'LOGIN':
                    self._send({'trnm':'REG','grp_no':'1','refresh':'1',
                                'data':[{'item':[''],'type':['00']}]})
                    self.state = 'REGISTER_WAIT'
                else:
                    self.state = 'RECEIVING'
                return []
            if kind != 'REAL' or self.state != 'RECEIVING':
                raise UncertainResponse('등록이 확인된 주문 알림이 아닙니다.')
            rows = frame.get('data')
            if not isinstance(rows,list) or not 1 <= len(rows) <= 100:
                raise UncertainResponse('주문 알림 목록이 불완전합니다.')
            received = self._now()
            notices, fingerprints = [], set()
            for row in rows:
                notice = order_notice(row, expected_account=self._expected_account,
                    profile_id=self._profile,session=self.session,session_started_at=self._started_at,
                    received_at=received,auth_generation=self._generation)
                fingerprint = hashlib.sha256(json.dumps(row,sort_keys=True).encode()).hexdigest()
                if fingerprint in self._seen or fingerprint in fingerprints:
                    continue
                notices.append(notice)
                fingerprints.add(fingerprint)
            if len(self._seen|fingerprints) > 4096:
                raise UncertainResponse('알림 보관 한도에 도달했습니다. 다시 확인해야 합니다.')
            self._guard()
            self._seen.update(fingerprints)
            return notices
        except Exception:
            self._fail()
            raise UncertainResponse('주문 알림 연결이 끊겼거나 확인이 필요해요. 잔고를 다시 조회해 주세요.') from None

    def accepts(self, notice):
        """Must be checked again before consuming an earlier notice."""
        if self.state != 'RECEIVING' or type(notice) is not OrderNotice:
            return False
        try:
            self._guard()
            return (notice.session == self.session and notice.account_tag == self._account_tag
                    and notice.auth_generation == self._generation and notice.profile_id == self._profile
                    and 0 <= (self._now()-notice.received_at).total_seconds() <= 120)
        except Exception:
            self._fail()
            return False

    def review_cancellation(self, journal, ledger, reviewer, root_key, notice, *,
                            known_orders, expected_deposit=None):
        """Explicit read-only follow-up; read_once never starts HTTP requests."""
        from app.kiwoom_mock_cancel_review import review_after_notice
        def guard():
            if not self.accepts(notice):
                raise UncertainResponse('알림 연결이 달라졌어요. 다시 확인해 주세요.')
        guard()
        if reviewer.transport is not self.transport or journal.transport is not self.transport:
            raise ContractError('같은 모의 연결의 조회·접수 기록이 필요합니다.')
        return review_after_notice(journal,ledger,reviewer,root_key,notice,
            expected_account=self._expected_account,session=self.session,known_orders=known_orders,
            expected_deposit=expected_deposit,connection_guard=guard)
    def _shutdown(self, state):
        socket, self._socket = self._socket, None
        self.state, self.session = state, None
        self._expected_account = self._account_tag = None
        self._seen.clear()
        if socket is not None:
            try:
                socket.close()
            except Exception:
                pass

    def _fail(self):
        self._shutdown('NEEDS_RECHECK')

    def close(self):
        self._shutdown('CLOSED')
