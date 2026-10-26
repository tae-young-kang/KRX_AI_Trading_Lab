"""Private manual price-refresh handoff to the PC owner, not a scheduler."""
from threading import Lock
from time import monotonic
from app.morning_quote_refresh import summary as result_summary


class MorningQuoteRequest:
    def __init__(self,mode,*,clock=monotonic):
        if mode not in ('DEMO','MOCK','REAL_QUOTES'):raise ValueError('가상 시세 모드를 확인해 주세요.')
        self.mode,self.clock=mode,clock;self.lock=Lock();self.state='IDLE'
        self.message='아직 시세 갱신 요청이 없어요.';self.notice=None;self.delivery='';self.generation=0;self.cancelled=False

    def _expire(self):
        if self.state=='PENDING' and not 0<=self.clock()-self.requested<120:
            self.state='FAILED';self.message='2분 안에 시작하지 못해 만료됐어요. /가상시세갱신 으로 다시 요청하세요.'

    def request(self):
        with self.lock:
            self._expire()
            if self.state in ('PENDING','RUNNING') or self.notice is not None:return '이미 가상 시세 갱신 요청이 있어요. /가상시세상태 로 확인하세요.'
            self.state='PENDING';self.requested=self.clock();self.cancelled=False;self.generation+=1;self.delivery=''
            self.message='기존 아침 가상 보유의 새 시세 조회를 요청했어요. 신규 매수나 계좌 주문이 아니에요.'
            return self.message

    def cancel(self):
        with self.lock:
            self.cancelled=True
            if self.state=='PENDING':self.state='CANCELED';self.message='대기 중인 가상 시세 조회를 중지했어요.'

    def running(self):
        with self.lock:return self.state=='RUNNING' and not self.cancelled

    def take(self,mode,*,busy,active):
        with self.lock:
            self._expire()
            if self.state!='PENDING':return False
            if not active or mode!=self.mode:
                self.state='CANCELED';self.message='수신·모드가 바뀌어 가상 시세를 조회하지 않았어요.';return False
            if busy:return False
            self.state='RUNNING';self.message='기존 가상 보유의 날짜·시장·시세를 확인하고 있어요.';return True

    def finish(self,*,success,result=None):
        with self.lock:
            if self.state!='RUNNING':return
            if success:
                self.state='CANCELED' if self.cancelled or result['canceled'] else 'DONE'
                self.message=('중지 전 일부 반영됐을 수 있어요.\n' if self.state=='CANCELED' else '가상 시세 확인을 마쳤어요.\n')+result_summary(result)
            else:
                self.state='FAILED';self.message='가상 시세 갱신을 완료하지 못했어요. 일부 유효 반영은 남을 수 있어요. /가상하루 로 기록을 확인하세요.'
            self.notice=(self.generation,self.message)

    def send_notice(self,sender,*,active):
        with self.lock:
            notice=self.notice
            if notice is None:return
            self.notice=None;self.delivery='알림 미확인 · 자동 재전송 안 함' if active else '수신 중지로 알림 생략'
        if not active:return
        try:receipt=sender('[가상 보유 시세 갱신]\n'+notice[1]);ok=type(receipt) is int and receipt>0
        except Exception:ok=False
        with self.lock:
            if self.generation==notice[0]:self.delivery='알림 발송 응답 확인' if ok else '알림 미확인 · 자동 재전송 안 함'

    def summary(self):
        with self.lock:
            self._expire()
            return f'[가상 보유 시세 갱신 · {self.mode}]\n{self.message}\n{self.delivery}'
