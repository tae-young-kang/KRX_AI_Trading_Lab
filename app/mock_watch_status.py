"""Sanitized, thread-safe status for Telegram; no Qt objects or credentials."""
from datetime import datetime
import threading
import time

from app.trading_clock import KST


class MockWatchStatus:
    def __init__(self, clock=None):
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

    def enable_refresh(self):
        with self._lock:
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
            self._refresh_enabled = self._refresh_pending = self._refresh_busy = False
            self._last_request = None
            self._refresh_state = '아직 새 잔고를 요청하지 않았어요.'
            self._positions = ()
            self._balance, self._stale = None, True
            self._notice = '아직 받은 알림이 없어요.'

    def verified(self, report):
        with self._lock:
            # Whitelisted display fields only; no account/token/profile IDs.
            self._balance = {key:report[key] for key in ('checked_at','order_money','held','pending')}
            self._positions = tuple(report.get('positions',()))
            self._stale = False

    def notice(self, text):
        with self._lock:
            self._notice, self._stale = text, True

    def stopped(self):
        with self._lock:
            if self._refresh_pending or self._refresh_busy:
                self._refresh_state = '잔고 조회를 완료하지 못했어요. 연결을 다시 확인해 주세요.'
            self._refresh_enabled = self._refresh_pending = self._refresh_busy = False
            self._stale = True
            self._status = '모의 알림 연결 종료 · 다시 확인 필요'

    def summary(self):
        with self._lock:
            lines = [f'프로그램이 켜져 있어요 · {self._clock().astimezone(KST):%H:%M:%S}',
                     '모의 연결: '+self._status, '실제 돈·키움 모의 주문: 잠금']
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
