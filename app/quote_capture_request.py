"""Private/GUI requests marshalled to one GUI-owned quote-only worker."""
from threading import Lock
from time import monotonic
from app.quote_capture import summary


class QuoteCaptureRequest:
    def __init__(self,root,*,clock=monotonic):
        self.root=root;self.clock=clock;self.lock=Lock();self.state='IDLE';self.owner=None;self.generation=0
        self.message='아직 시세 수집을 시작하지 않았어요.'

    def _expire(self):
        if self.state=='PENDING' and not 0<=self.clock()-self.at<120:
            self.state='CANCELED';self.message='수집 시작 기한이 지났어요. 다시 시작하려면 명시적으로 요청하세요.'

    def request(self,*,mode,owner=None):
        with self.lock:
            self._expire()
            if mode!='REAL_QUOTES':return '시세 수집은 실제 시세 보기 모드에서만 시작해요. 주문은 하지 않아요.'
            if self.state in ('PENDING','RUNNING'):return '이미 시세 수집이 대기/진행 중이에요. /시세수집상태 로 확인하세요.'
            self.state='PENDING';self.owner=owner;self.at=self.clock();self.generation+=1
            self.message='오늘 고정 KRX 목록의 시세 수집을 요청했어요. 첫 거래 인증·주문은 하지 않아요.';return self.message

    def take(self,*,mode,busy,active):
        with self.lock:
            if self.state!='PENDING':return None
            if not 0<=self.clock()-self.at<120 or mode!='REAL_QUOTES' or not active:
                self.state='CANCELED';self.message='시작 기한·수신·모드를 확인하지 못해 수집하지 않았어요.';return None
            if busy:return None
            self.state='RUNNING';self.message='시세 전용 수집 연결 중 · 공식 첫 거래 여부 미확인';return self.generation

    def active(self,generation):
        with self.lock:return self.state=='RUNNING' and self.generation==generation

    def cancel(self,owner=None,*,explicit=False):
        with self.lock:
            if owner is not None and self.owner!=owner and not (explicit and self.owner is None):return '이 수신 세션에서 시작한 수집이 아니에요.'
            self.state='CANCELED';self.message='수집 중지를 요청했어요. 저장된 근거는 보존해요.';return self.message

    def finish(self,generation,state):
        with self.lock:
            if generation!=self.generation:return
            self.state='FAILED' if state=='FAILED' else 'STOPPED'
            self.message='수집 중단 · 자료/연결 확인 필요' if state=='FAILED' else '시세 수집을 중지했어요. 다시 시작하려면 명시적으로 요청하세요.'

    def report(self):
        with self.lock:
            self._expire();text=self.message
        return text+'\n'+summary(self.root)
