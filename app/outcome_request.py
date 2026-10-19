"""One bounded Telegram-to-PC cached outcome review; no clients or orders."""
from threading import Lock
from time import monotonic
from datetime import datetime
from app.trading_clock import KST
from app.recommendation_import import evaluation_counts


class OutcomeRequest:
    def __init__(self,mode,*,source=lambda:None,clock=monotonic,wall_clock=lambda:datetime.now(KST)):
        self.mode,self.source,self.clock,self.wall_clock=mode,source,clock,wall_clock
        self.lock=Lock();self.state='IDLE';self.notice=None;self.generation=0
        self.message='아직 추천 성적 갱신 요청이 없어요.'

    def expire(self):
        if self.state=='PENDING' and not 0<=self.clock()-self.started<120:
            self.state='FAILED';self.message='요청이 만료됐어요. /성적갱신으로 다시 요청하세요.'

    def request(self,*,collect=False):
        with self.lock:
            self.expire()
            if self.state in ('PENDING','RUNNING') or self.notice is not None:
                return '이미 갱신 요청이 있어요. /성적갱신상태로 확인하세요.'
            source=self.source()
            if self.mode=='DEMO':return '가상 연습 성적은 PC의 고급 자료 평가를 이용해 주세요.'
            if self.mode not in ('MOCK','REAL_QUOTES') or source not in (None,'KRX','CSV_USER'):
                return '추천 평가의 모드·자료 출처를 확인해 주세요.'
            if collect and source=='CSV_USER':return 'CSV 자료는 자동 수집으로 바꾸지 않아요. PC에서 KRX 자료를 선택해 주세요.'
            moment=self.wall_clock()
            if moment.tzinfo is None:return 'PC의 시간대를 확인해 주세요.'
            self.data=dict(source=source,today=moment.astimezone(KST).date(),collect=collect)
            self.generation+=1
            self.started=self.clock();self.state='PENDING'
            self.message='집 PC에 성적 갱신을 요청했어요. 저장 자료만 사용해요. /성적갱신상태로 확인하세요.'
            if collect:self.message='집 PC에 빠진 KRX 일봉 보완을 요청했어요. 최대 60개 시장·날짜만 조회한 뒤 다시 평가해요. /성적갱신상태로 확인하세요.'
            return self.message

    def take(self,mode,*,busy,active):
        with self.lock:
            self.expire()
            if self.state!='PENDING':return None
            if not active or mode!=self.mode:
                self.state='FAILED';self.message='수신 또는 모드가 바뀌어 평가하지 않았어요.'
                return None
            if busy:return None
            self.state='RUNNING';self.message='저장된 일봉으로 추천 후 가격을 확인하고 있어요.'
            if self.data.get('collect'):self.message='빠진 KRX 일봉을 확인하고 추천 성적을 다시 계산하고 있어요.'
            return dict(self.data)

    def finish(self,*,success,result=None):
        with self.lock:
            if self.state!='RUNNING':return
            self.state='DONE' if success else 'FAILED'
            self.message=(f'추천 {result["recommendations"]}개 확인 · 새 평가 {result["added"]}개\n'
                          f'자료 확인일 {result["available_through"]}\n/추천성적에서 확인하세요. 실제 매매 수익률은 아니에요.'
                          if success else '성적을 갱신하지 못했어요. PC에서 평가 자료와 거래일 설정을 확인해 주세요.')
            if not success and self.data.get('collect'):
                self.message='자료 보완에 실패했어요. PC의 별도 KRX 인증키·서비스 승인과 인터넷 연결을 확인해 주세요. 저장한 일봉은 남아 있어요.'
            if success and evaluation_counts(result):self.message+='\n'+evaluation_counts(result)
            if success and result.get('collection'):
                collection=result['collection']
                self.message+=f'\n자료 보완 조회 {collection["requests"]}회 · 저장 {collection["rows"]}행'
                if collection['remaining_targets']:
                    self.message+=f'\n조회 한도 때문에 이번에 확인하지 못한 시장·날짜 {collection["remaining_targets"]}개'
            self.notice=self.message

    def send_notice(self,sender,*,active):
        with self.lock:
            message=self.notice;self.notice=None;generation=self.generation
        if message is None or not active:return
        try:
            receipt=sender('[추천 성적 갱신]\n'+message)
            confirmed=type(receipt) is int and receipt>0
        except Exception:confirmed=False
        with self.lock:
            if generation==self.generation:
                self.message+='\n'+('완료 알림 응답 확인' if confirmed else '완료 알림 미확인 · 자동 재전송 안 함')

    def summary(self):
        with self.lock:
            self.expire()
            return '[추천 성적 갱신]\n'+self.message+'\n자료 수집은 /성적자료보완을 요청한 경우에만 해요. 주문은 하지 않아요.'
