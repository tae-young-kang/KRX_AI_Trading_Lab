"""In-memory handoff from Telegram to the GUI; no clients or orders."""
from threading import Lock
from time import monotonic


class RecommendationRequest:
    def __init__(self, mode, *, clock=monotonic):
        self.mode = mode
        self._clock = clock
        self._requested_at = None
        self._lock = Lock()
        self._state = 'IDLE'
        self._message = '아직 새 검색 요청이 없어요.'
        self._generation = 0
        self._notice = None
        self._delivery = ''

    def request(self):
        with self._lock:
            self._expire_pending()
            if self._state in ('PENDING', 'RUNNING'):
                return '이미 검색 요청이 있어요. /검색상태 로 확인하세요.'
            if self._notice is not None:
                return '완료 알림을 준비 중이에요. 잠시 뒤 /검색상태 를 확인하세요.'
            self._generation += 1
            self._delivery = ''
            self._state = 'PENDING'
            self._requested_at = self._clock()
            self._message = '집 PC에 새 검색을 요청했어요. 2분 안에 시작하지 못하면 만료돼요. /검색상태 로 확인하세요.'
            return self._message

    def take(self, mode, *, busy, active):
        with self._lock:
            self._expire_pending()
            if self._state != 'PENDING':
                return False
            if not active or mode != self.mode:
                self._state = 'FAILED'
                self._message = '수신 또는 모드가 바뀌어 검색하지 않았어요. 다시 요청해 주세요.'
                return False
            if busy:
                return False
            self._state = 'RUNNING'
            self._message = '선택 시장의 종목을 조회 중이에요. 완료 후 /추천기록 을 확인하세요.'
            return True

    def _expire_pending(self):
        if self._state == 'PENDING':
            age = self._clock() - self._requested_at
            if not 0 <= age < 120:
                self._state = 'FAILED'
                self._message = '대기 시간이 지나 검색 요청이 만료됐어요. /추천검색 으로 다시 요청해 주세요.'

    def progress(self, message):
        with self._lock:
            if self._state == 'RUNNING' and isinstance(message, str):
                detail = ''.join(c for c in message if c.isprintable())[:240]
                self._message = '조회 중 · '+detail

    def finish(self, *, success, result=''):
        with self._lock:
            if self._state == 'RUNNING':
                self._state = 'DONE' if success else 'FAILED'
                self._message = ('검색 결과를 저장했어요. /추천기록 으로 확인하세요.' if success else
                                 '검색을 완료하지 못했어요. 집 PC의 연결·검색 상태를 확인해 주세요.')
                self._notice = (self._generation, f'[{self.mode} · 추천 검색]\n{self._message}'
                                + ('\n'+result if success and result else ''))
                self._delivery = '완료 알림 준비 중'

    def send_notice(self, sender, *, active):
        """Claim once before IO. Unknown outcomes never trigger an automatic retry."""
        with self._lock:
            notice = self._notice
            if notice is None:
                return
            self._notice = None
            self._delivery = '완료 알림 응답 미확인 · 자동 재전송 안 함' if active else '수신 중지로 완료 알림 생략'
        if not active:
            return
        try:
            receipt = sender(notice[1])
            confirmed = type(receipt) is int and receipt > 0
        except Exception:
            confirmed = False
        with self._lock:
            if self._generation == notice[0]:
                self._delivery = ('완료 알림 발송 응답 확인 · 읽음 확인은 아님' if confirmed else
                                  '완료 알림 응답 미확인 · 자동 재전송 안 함')

    def summary(self):
        with self._lock:
            self._expire_pending()
            return f'[{self.mode} · 추천 검색]\n{self._message}\n{self._delivery}\n계좌 주문은 하지 않아요.'
