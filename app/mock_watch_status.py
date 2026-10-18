"""Sanitized, thread-safe status for Telegram; no Qt objects or credentials."""
from datetime import datetime
import threading

from app.trading_clock import KST


class MockWatchStatus:
    def __init__(self, clock=None):
        self._clock = clock or (lambda:datetime.now(KST))
        self._lock = threading.RLock()
        self._status = '모의 알림 연결 전'
        self._balance = None
        self._stale = True
        self._notice = '아직 받은 알림이 없어요.'

    def status(self, text):
        with self._lock:
            self._status = text

    def reset(self):
        with self._lock:
            self._balance, self._stale = None, True
            self._notice = '아직 받은 알림이 없어요.'

    def verified(self, report):
        with self._lock:
            # Whitelisted display fields only; no account/token/profile IDs.
            self._balance = {key:report[key] for key in ('checked_at','order_money','held','pending')}
            self._stale = False

    def notice(self, text):
        with self._lock:
            self._notice, self._stale = text, True

    def stopped(self):
        with self._lock:
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
                if self._stale:
                    lines.append('이 잔고는 최신 값이 아닐 수 있어요. 다시 확인해 주세요.')
            lines.extend(['최근 모의 알림: '+self._notice,
                          '/조건: 추천 기준 보기 · 이 명령으로 주문하지 않아요.'])
            return '\n'.join(lines)
