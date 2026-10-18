"""Bounded, sanitized in-memory observations, never execution certification."""
from collections import deque
from app.trading_clock import KST


LABELS = {
    'START':'모의 연결 확인 시작', 'ACCOUNT':'모의 계좌·잔고 조회 관측',
    'STREAM':'모의 알림 등록 확인 관측', 'REFRESH':'요청한 잔고 재조회 관측',
    'NOTICE':'모의 주문 처리 알림 관측', 'STOP':'모의 연결 종료',
    'RECOVERY':'사용자가 모의 재연결 요청',
}


class MockValidationProgress:
    """Owned by MockWatchStatus's lock; stores no broker payload or secrets."""
    def __init__(self, clock):
        self.clock = clock
        self.events = deque(maxlen=20)
        self.account = self.stream = False
        self.refreshes = self.notices = 0
        self.account_at = self.stream_at = self.refresh_at = None

    def record(self, event):
        label = LABELS[event]  # Only fixed event labels, never server text.
        moment=self.clock()
        if moment.tzinfo is None:
            raise ValueError('모의 점검 시각에 시간대가 필요해요.')
        self.events.append((moment.astimezone(KST).strftime('%m/%d %H:%M:%S'),label))
        if event == 'START':
            self.account = self.stream = False
            self.refreshes = self.notices = 0
            self.account_at = self.stream_at = self.refresh_at = None
        elif event == 'ACCOUNT':
            self.account = True
            self.account_at = moment
        elif event == 'STREAM':
            self.stream = True
            self.stream_at = moment
        elif event == 'REFRESH':
            self.refreshes += 1
            self.refresh_at = moment
        elif event == 'NOTICE': self.notices += 1
        elif event == 'STOP': self.stream = False

    def summary(self):
        now=self.clock()
        if now.tzinfo is None:
            raise ValueError('모의 점검 시각에 시간대가 필요해요.')
        def same_day(stamp):
            return (stamp is not None and stamp<=now
                    and stamp.astimezone(KST).date()==now.astimezone(KST).date())
        account=self.account and same_day(self.account_at)
        stream=self.stream and same_day(self.stream_at)
        if not account:
            next_step='집 PC에서 키움 모의투자 모드를 확인하고 오늘의 모의 계좌·잔고를 새로 확인하세요.'
        elif not stream:
            next_step='집 PC에서 모의 알림 연결 상태를 확인하세요. 새 주문 허용과는 별개입니다.'
        elif not same_day(self.refresh_at) or (now-self.refresh_at).total_seconds()>30:
            next_step='/잔고로 새 조회를 요청하고 /잔고결과에서 완료 시각을 확인하세요.'
        else:
            next_step='같은 모의 계좌의 HTS/MTS 잔고·미체결과 조회값을 직접 비교하세요. 주문 검증은 별도입니다.'
        lines = ['[모의 연결 점검 · 현재 실행의 관측]',
                 '실제 돈 주문은 잠겨 있어요. 키움 모의 주문은 PC의 별도 당일 허용이 필요해요.',
                 '다음 할 일: '+next_step,
                 '계좌·잔고 조회: '+('관측됨' if account else '아직 관측 없음 · 오늘 재확인 필요'),
                 '알림 등록: '+('관측됨' if stream else '연결 전 또는 종료'),
                 f'이번 연결의 새 잔고 조회 {self.refreshes}회 · 주문 처리 알림 {self.notices}회',
                 '알림은 체결 완료·잔고 일치 증명이 아니에요.',
                 '조회 관측 기록은 최신 잔고라는 뜻이 아니에요. 조회 시각을 확인해 주세요.',
                 '날짜가 바뀌거나 시계가 뒤로 가면 이전 관측을 오늘의 준비 상태로 사용하지 않아요.',
                 '남은 확인: 모의 매수·매도·정정·취소·부분체결, HTS/MTS 잔고 대조, 비용·결제 확인',
                 '이 점검으로 모의 체결 검증 완료나 실계좌 주문 허용을 판정하지 않아요.',
                 '최근 관측(최대 10개 · 프로그램 종료 시 사라짐):']
        lines.extend(f'{stamp} · {label}' for stamp,label in list(self.events)[-10:])
        if not self.events: lines.append('아직 기록이 없어요. 집 PC에서 모의 연결을 시작해 주세요.')
        lines.append('/모의상태: 마지막 상태 · /잔고: 새 조회 요청')
        return '\n'.join(lines)
