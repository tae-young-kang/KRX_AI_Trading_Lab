"""One local comparison job handed to the PC owner; no IO clients/orders."""
from threading import Lock
from time import monotonic


class ConditionAnalysisRequest:
    def __init__(self,mode,report,*,clock=monotonic,review=None):
        if mode not in ('DEMO','MOCK','REAL_QUOTES'):raise ValueError('조건 비교 모드를 확인해 주세요.')
        self.mode,self.report,self.clock=mode,report,clock
        self.review=review
        self.lock=Lock();self.state='IDLE';self.message='아직 조건 비교 요청이 없어요.'
        self.notice=None;self.delivery='';self.generation=0

    def _expire(self):
        if self.state=='PENDING' and not 0<=self.clock()-self.requested<120:
            self.state='FAILED';self.message='2분 안에 시작하지 못해 만료됐어요. /조건비교로 다시 요청하세요.'

    def request(self):
        with self.lock:
            self._expire()
            if self.state in ('PENDING','RUNNING'):return '이미 비교 요청이 있어요. /조건비교상태로 확인하세요.'
            if self.notice is not None:return '조건 비교 완료 알림을 준비 중이에요. 잠시 뒤 다시 요청하세요.'
            self.state='PENDING';self.requested=self.clock();self.generation+=1;self.delivery=''
            self.message='집 PC에 저장된 가상 매매의 조건 비교를 요청했어요. 조건을 적용하거나 주문하지 않아요.'
            return self.message

    def take(self,mode,*,busy,active):
        with self.lock:
            self._expire()
            if self.state!='PENDING':return False
            if mode!=self.mode or not active:
                self.state='FAILED';self.message='수신·모드가 바뀌어 비교하지 않았어요.';return False
            if busy:return False
            self.state='RUNNING';self.message='저장된 가상 기록을 기존 기준으로 비교하고 있어요.'
            return True

    def finish(self,*,success,result=''):
        with self.lock:
            if self.state!='RUNNING':return
            self.state='DONE' if success else 'FAILED'
            self.message=('조건 비교를 마쳤어요. '+result+'\n/개선안 · /조건성적' if success else
                '조건 비교를 완료하지 못했어요. PC 기록을 확인해 주세요. 조건을 적용하지 않았어요.')
            self.notice=(self.generation,'[가상 조건 비교]\n'+self.message)

    def send_notice(self,sender,*,active):
        with self.lock:
            notice=self.notice
            if notice is None:return
            self.notice=None;self.delivery='알림 미확인 · 자동 재전송 안 함' if active else '수신 중지로 알림 생략'
        if not active:return
        try:
            receipt=sender(notice[1]);ok=type(receipt) is int and receipt>0
        except Exception:ok=False
        with self.lock:
            if notice[0]==self.generation:self.delivery='알림 발송 응답 확인' if ok else '알림 미확인 · 자동 재전송 안 함'

    def summary(self):
        with self.lock:
            self._expire()
            return f'[가상 조건 비교 · {self.mode}]\n{self.message}\n{self.delivery}\nAI 학습·독립 미래 검증은 아니며 자동 적용/주문하지 않아요.'
