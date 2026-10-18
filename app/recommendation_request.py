"""In-memory handoff from Telegram to the GUI; no clients or orders."""
from threading import Lock


class RecommendationRequest:
    def __init__(self, mode):
        self.mode = mode
        self._lock = Lock()
        self._state = 'IDLE'
        self._message = '아직 새 검색 요청이 없어요.'

    def request(self):
        with self._lock:
            if self._state in ('PENDING', 'RUNNING'):
                return '이미 검색 요청이 있어요. /검색상태 로 확인하세요.'
            self._state = 'PENDING'
            self._message = '집 PC에 새 검색을 요청했어요. /검색상태 로 확인하세요.'
            return self._message

    def take(self, mode, *, busy, active):
        with self._lock:
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

    def finish(self, *, success):
        with self._lock:
            if self._state == 'RUNNING':
                self._state = 'DONE' if success else 'FAILED'
                self._message = ('검색 결과를 저장했어요. /추천기록 으로 확인하세요.' if success else
                                 '검색을 완료하지 못했어요. 집 PC의 연결·검색 상태를 확인해 주세요.')

    def summary(self):
        with self._lock:
            return f'[{self.mode} · 추천 검색]\n{self._message}\n계좌 주문은 하지 않아요.'
