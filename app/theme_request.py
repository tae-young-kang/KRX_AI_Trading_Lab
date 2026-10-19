"""One cached Theme1 request per receiver; no network, credentials or orders."""
from threading import Lock
from time import monotonic

from app.theme_input import saved_selection


class ThemeRequest:
    def __init__(self,history,mode,*,clock=monotonic):
        self.history,self.mode,self.clock=history,mode,clock
        self.lock=Lock()
        self.state='IDLE'
        self.data=None
        self.message='아직 테마1 검색 요청이 없어요.'

    def _expire(self):
        if self.state=='PENDING' and not 0<=self.clock()-self.started<120:
            self.state='FAILED'
            self.message='2분 안에 시작하지 못해 만료됐어요. /테마1검색으로 다시 요청하세요.'

    def request(self):
        with self.lock:
            self._expire()
            if self.state in ('PENDING','RUNNING'):
                return '이미 테마1 검색 요청이 있어요. /테마1검색상태로 확인하세요.'
            try:
                data=saved_selection(self.history,self.mode)
            except ValueError as error:
                return str(error)
            self.data=data
            self.started=self.clock()
            self.state='PENDING'
            self.message=(f'테마1 판정 요청 · 자료 {data["source"]} · 기준일 {data["through"]}\n'
                          'PC에서 확인한 날짜 그대로 저장 시세를 판정해요. 오늘로 날짜를 자동 연장하지 않아요.\n'
                          '새 시세 수집은 하지 않아요. /테마1검색상태로 확인하세요.')
            return self.message

    def take(self,mode,*,busy,active):
        with self.lock:
            self._expire()
            if self.state!='PENDING':return None
            if not active or mode!=self.mode:
                self.state='FAILED'
                self.message='수신 또는 모드가 바뀌어 판정하지 않았어요. 다시 요청하세요.'
                return None
            if busy:return None
            self.state='RUNNING'
            self.message=f'기준일 {self.data["through"]} · 저장 시세로 테마1 판정 중이에요.'
            return self.data

    def finish(self,*,success,result=None):
        with self.lock:
            if self.state!='RUNNING':return
            self.state='DONE' if success else 'FAILED'
            self.message=(f'기준일 {self.data["through"]} · 감지 {result["matched"]}개 · 자료 부족 '
                          f'{result["states"].get("PRICE_MISSING",0)}개\n/테마1에서 결과를 보세요. 전체 시장 수집 완료는 미확인이에요.'
                          if success else '테마1 판정을 완료하지 못했어요. PC에서 저장 시세·거래일 설정을 확인하세요.')

    def summary(self):
        with self.lock:
            self._expire()
            return '[테마1 검색 상태]\n'+self.message+'\n종목별 알림 발송·계좌 주문은 실행하지 않아요.'
