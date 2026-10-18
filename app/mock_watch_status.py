"""Sanitized, thread-safe status for Telegram; no Qt objects or credentials."""
from datetime import datetime
import threading
import time

from app.trading_clock import KST
from app.mock_validation import MockValidationProgress
from app.mock_observation_history import MockObservationHistory


class MockWatchStatus:
    def __init__(self, clock=None, *, history_path=None):
        self._clock = clock or (lambda:datetime.now(KST))
        self._lock = threading.RLock()
        self._status = '모의 알림 연결 전'
        self._balance = None
        self._stale = True
        self._notice = '아직 받은 알림이 없어요.'
        self._refresh_enabled = False
        self._refresh_state = '아직 새 잔고를 요청하지 않았어요.'
        self._refresh_pending = False
        self._refresh_busy = False
        self._last_request = None
        self._positions = ()
        self._reconnect_available = self._reconnect_pending = False
        self._last_reconnect = None
        self._validation = MockValidationProgress(self._clock)
        self._orders = None
        self._history = MockObservationHistory(history_path)

    def history_summary(self):
        with self._lock:
            return self._history.summary()

    def _record(self, event):
        self._validation.record(event)
        self._history.record(event, self._clock())

    def bind_orders(self, orders):
        with self._lock:
            self._orders = orders

    def handle_orders(self, text, **context):
        with self._lock:
            orders = self._orders
        if not orders:
            return '키움 모의 주문은 잠겨 있어요. 집 PC에서 당일 모의 주문 허용을 확인해 주세요.'
        reply=orders.handle(text,**context)
        if text=='/긴급중지':
            with self._lock:
                self._stale=True
                self._notice='키움 모의 긴급중지 요청을 기록했어요. /주문점검과 모의 HTS/MTS에서 확인해 주세요.'
                self._history.record('HALT', self._clock())
        return reply

    def cancel_orders(self, scope, session):
        with self._lock:
            orders = self._orders
        if orders: orders.cancel_session(scope,session)

    def validation_summary(self):
        with self._lock:
            return self._validation.summary()

    def offer_reconnect(self):
        with self._lock:
            self.stopped()
            self._reconnect_available = True
            self._status = '모의 연결 끊김 · /모의재연결로 다시 연결할 수 있어요.'

    def request_reconnect(self):
        with self._lock:
            if self._reconnect_pending:
                return '이미 모의 재연결을 요청했어요. /모의상태로 확인해 주세요.'
            if not self._reconnect_available:
                return '지금은 원격 재연결할 수 없어요. 집 PC의 모의 연결과 원격 복구 허용을 확인해 주세요.'
            moment = time.monotonic()
            if self._last_reconnect is not None and moment-self._last_reconnect < 30:
                return '재연결 요청 간격은 30초예요. /모의상태로 확인해 주세요.'
            self._last_reconnect = moment
            self._reconnect_pending = True
            self._record('RECOVERY')
            self._status = '모의 재연결 요청 대기 · 주문은 보내지 않아요.'
            return self._status

    def take_reconnect(self):
        with self._lock:
            if not self._reconnect_available or not self._reconnect_pending:
                return False
            self._reconnect_available = self._reconnect_pending = False
            self._status = '모의 계좌부터 다시 확인하고 있어요. 주문은 보내지 않아요.'
            return True

    def enable_refresh(self):
        with self._lock:
            if not self._refresh_enabled:
                self._record('STREAM')
            self._refresh_enabled = True

    def request_refresh(self):
        with self._lock:
            if not self._refresh_enabled:
                return '모의 연결이 필요해요. 집 PC에서 모의 계좌 확인과 알림 시작을 먼저 해 주세요.'
            if self._refresh_pending or self._refresh_busy:
                return '이미 모의 잔고를 확인 중이에요. /잔고결과로 확인해 주세요.'
            now = time.monotonic()
            if self._last_request is not None and now-self._last_request < 30:
                return '조회 간격은 30초예요. /잔고결과로 마지막 결과를 확인해 주세요.'
            self._last_request = now
            self._refresh_pending = True
            self._refresh_state = '모의 잔고 확인 중이에요. /잔고결과로 확인해 주세요.'
            self._stale = True
            return self._refresh_state

    def take_refresh(self):
        with self._lock:
            if not self._refresh_enabled or not self._refresh_pending:
                return False
            self._refresh_pending, self._refresh_busy = False, True
            return True

    def finish_refresh(self, report):
        with self._lock:
            self.verified(report)
            self._record('REFRESH')
            self._refresh_busy = False
            self._refresh_state = '모의 잔고 새 조회 완료 · 조회 시점의 값이에요.'

    def refresh_result(self):
        with self._lock:
            return self._refresh_state+'\n'+self.summary()

    def status(self, text):
        with self._lock:
            self._status = text

    def reset(self):
        with self._lock:
            self._orders = None
            self._record('START')
            self._reconnect_available = self._reconnect_pending = False
            self._refresh_enabled = self._refresh_pending = self._refresh_busy = False
            self._last_request = None
            self._refresh_state = '아직 새 잔고를 요청하지 않았어요.'
            self._positions = ()
            self._balance, self._stale = None, True
            self._notice = '아직 받은 알림이 없어요.'

    def verified(self, report):
        with self._lock:
            self._record('ACCOUNT')
            # Whitelisted display fields only; no account/token/profile IDs.
            self._balance = {key:report[key] for key in ('checked_at','order_money','held','pending')}
            self._positions = tuple(report.get('positions',()))
            self._stale = False

    def notice(self, text):
        with self._lock:
            self._record('NOTICE')
            self._notice, self._stale = text, True

    def order_result(self, state):
        with self._lock:
            if state in ('ACKNOWLEDGED','REJECTED','UNKNOWN','FAILED'):
                self._history.record(state, self._clock())
            self._stale = True
            self._notice = '모의 주문 처리 결과가 있어요. /주문상태와 /잔고로 확인해 주세요.'

    def order_warning(self):
        with self._lock:
            self._history.record('WARNING', self._clock())
            self._stale = True
            self._notice = '모의 기록 충돌로 주문을 차단했어요. /주문점검으로 이유를 확인해 주세요.'

    def stopped(self):
        with self._lock:
            self._record('STOP')
            self._reconnect_available = self._reconnect_pending = False
            if self._refresh_pending or self._refresh_busy:
                self._refresh_state = '잔고 조회를 완료하지 못했어요. 연결을 다시 확인해 주세요.'
            self._refresh_enabled = self._refresh_pending = self._refresh_busy = False
            self._stale = True
            self._status = '모의 알림 연결 종료 · 다시 확인 필요'

    def summary(self):
        with self._lock:
            lines = [f'프로그램이 켜져 있어요 · {self._clock().astimezone(KST):%H:%M:%S}',
                     '모의 연결: '+self._status, '실제 돈·키움 모의 주문: 잠금' if not self._orders else
                     '실제 돈 주문: 잠금 · 키움 모의 주문 허용 여부는 /주문상태와 PC에서 확인']
            if self._balance is None:
                lines.append('아직 확인한 모의 잔고가 없어요.')
            else:
                b = self._balance
                money = '다시 확인 필요' if b['order_money'] is None else f"{b['order_money']:,}원"
                lines.append(f"마지막 잔고 확인 {b['checked_at']} · 보유 {b['held']:,}주 · 남은 주문 {b['pending']:,}주")
                lines.append('그때 조회한 주문 가능 금액: '+money)
                if self._positions:
                    lines.append('보유 종목(코드): '+', '.join(f'{code} {qty:,}주' for code,qty in self._positions[:15]))
                    if len(self._positions) > 15:
                        lines.append(f'외 {len(self._positions)-15}종목 · 상세 목록 표시 생략')
                if self._stale:
                    lines.append('이 잔고는 최신 값이 아닐 수 있어요. 다시 확인해 주세요.')
            lines.extend(['최근 모의 알림: '+self._notice,
                          '/조건: 추천 기준 보기 · 이 명령으로 주문하지 않아요.'])
            return '\n'.join(lines)
