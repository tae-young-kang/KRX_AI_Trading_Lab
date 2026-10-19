"""One cached Theme1 request per receiver; no network, credentials or orders."""
from threading import Lock
from time import monotonic

from app.theme_input import saved_selection
from app.theme_calendar import request_selection
from app.theme_store import missing_data_notice
from app.trading_clock import KST
from datetime import datetime
from app.theme_sources import KIWOOM_SOURCES


class ThemeRequest:
    def __init__(self,history,mode,*,clock=monotonic,wall_clock=None):
        self.history,self.mode,self.clock=history,mode,clock
        self.lock=Lock()
        self.state='IDLE'
        self.data=None
        self.message='아직 테마1 검색 요청이 없어요.'
        self.wall_clock=wall_clock or (lambda:datetime.now(KST))
        self._notice=None
        self._delivery=''

    def _expire(self):
        if self.state=='PENDING' and not 0<=self.clock()-self.started<120:
            self.state='FAILED'
            self.message='2분 안에 시작하지 못해 만료됐어요. /테마1검색으로 다시 요청하세요.'

    def request(self):
        with self.lock:
            self._expire()
            if self.state in ('PENDING','RUNNING'):
                return '이미 테마1 검색 요청이 있어요. /테마1검색상태로 확인하세요.'
            if self._notice is not None:return '테마1 완료 알림을 준비 중이에요. 잠시 뒤 다시 요청하세요.'
            try:
                try:
                    source=saved_selection(self.history,self.mode)['source']
                except ValueError:
                    source='DEMO' if self.mode=='DEMO' else KIWOOM_SOURCES[self.mode]
                if source=='KRX':source=KIWOOM_SOURCES[self.mode]
                data=request_selection(self.mode,source,now=self.wall_clock())
            except ValueError as error:
                return str(error)
            self.data=data
            self.started=self.clock()
            self.state='PENDING'
            self._delivery=''
            self.message=(f'테마1 판정 요청 · 자료 {data["source"]} · 기준일 {data["through"]}\n'
                          '요청일 기준 최근 6거래일과 전일 종가용 1일을 자동 계산했어요.\n'
                          +('키움 시세 조회 후 감지 종목을 한 종목씩 알려드려요.\n' if source in KIWOOM_SOURCES.values() else '저장 시세로 판정해요.\n')+
                          '/테마1검색상태로 확인하세요.')
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
            action=('키움 시세를 조회하고 테마1 종목을 찾고 있어요.'
                    if self.data['source'] in KIWOOM_SOURCES.values() else '저장 시세로 테마1 판정 중이에요.')
            self.message=f'기준일 {self.data["through"]} · '+action
            return self.data

    def finish(self,*,success,result=None):
        with self.lock:
            if self.state!='RUNNING':return
            self.state='DONE' if success else 'FAILED'
            if success and missing_data_notice(result):
                partial=result.get('matched',0)>0
                self.state='PARTIAL' if partial else 'NEEDS_DATA'
                self.message=missing_data_notice(result)
                if partial:self.message+=f'\n확인된 감지 종목 {result["matched"]}개는 따로 알려드려요.'
                self._notice=dict(message=self.message,run_id=result.get('run_id') if partial else None)
                return
            self.message=(f'기준일 {self.data["through"]} · 감지 {result["matched"]}개 · 자료 부족 '
                          f'{result["states"].get("PRICE_MISSING",0)}개\n/테마1에서 결과를 보세요. 전체 시장 수집 완료는 미확인이에요.'
                          if success else '테마1 검색을 완료하지 못했어요. PC의 오류 안내를 확인한 뒤 /테마1검색으로 다시 요청하세요.')
            self._notice=dict(message=self.message,run_id=result.get('run_id') if success else None)

    def send_notice(self,transport,chat_id,*,active,stopped=lambda:False):
        with self.lock:
            notice=self._notice
            if notice is None:return
            self._notice=None
            self._delivery='알림 응답 미확인 · 자동 재전송 안 함' if active else '수신 중지로 알림 생략'
        if not active or stopped():return
        try:
            receipt=transport.send(chat_id,'[테마1 검색 결과]\n'+notice['message'])
            if type(receipt) is not int or receipt<=0:return
            if notice['run_id'] is not None:
                from app.theme_delivery import deliver_run
                result=deliver_run(self.history,mode=self.mode,run_id=notice['run_id'],transport=transport,chat_id=chat_id,stopped=stopped)
                message=f'종목 알림 응답 확인 {result["SENT"]}개 · 미확인 {result["UNKNOWN"]}개 · 남은 {result["remaining"]}개'
            else:message='결과 안내 발송 응답 확인'
        except Exception:return
        with self.lock:self._delivery=message

    def summary(self):
        with self.lock:
            self._expire()
            return '[테마1 검색 상태]\n'+self.message+'\n'+self._delivery+'\n계좌 주문은 실행하지 않아요.'
