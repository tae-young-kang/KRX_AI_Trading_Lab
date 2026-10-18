"""Read-only UI session. No dispatch journal, order engine, or Telegram calls."""
from datetime import datetime
import re

from app.kiwoom_mock_contract import ContractError, UncertainResponse
from app.kiwoom_mock_review import MockAccountReviewer
from app.kiwoom_mock_status import money_view
from app.kiwoom_mock_stream import MockOrderStream
from app.kiwoom_mock_transport import MockTransport
from app.modes import current_mode
from app.trading_clock import KST


def require_mock_mode(config):
    if current_mode(config).key != 'MOCK':
        raise ContractError('키움 모의투자 모드를 선택하고 저장해 주세요.')


class MockWatchSession:
    ready_for_orders = False

    def __init__(self, transport, *, stream_factory=MockOrderStream):
        if type(transport) is not MockTransport or transport.environment != 'MOCK':
            raise ContractError('모의 전용 연결이 필요합니다.')
        self.transport = transport
        self._stream_factory = stream_factory
        self.stream = None
        self._account = None
        self._generation = self._profile = None
        self._verified_at = None
        self.state = 'NEW'
        self.notice_count = 0
        self.balance_stale = True

    def _now(self):
        moment = self.transport._clock()
        if not isinstance(moment,datetime) or moment.tzinfo is None or moment.utcoffset() is None:
            raise ContractError('확인 시간을 다시 확인해 주세요.')
        return moment.astimezone(KST)

    def verify(self, expected_account):
        if self.state != 'NEW' or not isinstance(expected_account,str) or not re.fullmatch(r'[0-9]{10}',expected_account):
            raise ContractError('새 연결에서 모의 계좌번호 10자리를 확인해 주세요.')
        try:
            capture = MockAccountReviewer(self.transport).capture(expected_account=expected_account,
                                                               trade_date=self.transport.current_day)
            self._account = expected_account
            self._generation, self._profile = capture.auth_generation, capture.profile_id
            self._verified_at = capture.finished_at
            self.state, self.balance_stale = 'VERIFIED', False
            funds = money_view(capture.observations,{})
            # Unknown/unmapped existing orders never masquerade as zero exposure.
            return dict(title='모의 계좌와 잔고를 확인했어요',checked_at=capture.finished_at.strftime('%m/%d %H:%M:%S'),
                order_money=funds.order_money,held=sum(row[1] for row in capture.observations.holdings),
                pending=sum(row[3] for row in capture.observations.unfilled),ready_for_orders=False)
        except Exception:
            self.close()
            raise UncertainResponse('모의 계좌를 확인하지 못했어요. 연결 정보와 계좌번호를 다시 확인해 주세요.') from None

    def _guard(self, *, starting=False):
        self.transport.ready()
        moment = self._now()
        if (self.transport.auth_generation != self._generation or self.transport.profile_id != self._profile
                or moment.date() != self._verified_at.date() or moment < self._verified_at
                or (starting and (moment-self._verified_at).total_seconds() > 120)):
            raise UncertainResponse('계좌 확인이 오래됐거나 연결이 달라졌어요. 다시 확인해 주세요.')

    def refresh(self):
        if self.state != 'RECEIVING':
            raise ContractError('모의 알림 연결을 먼저 확인해 주세요.')
        try:
            self.balance_stale = True
            self._guard()
            capture = MockAccountReviewer(self.transport).capture(
                expected_account=self._account, trade_date=self.transport.current_day)
            self._guard()
            if capture.auth_generation != self._generation or capture.profile_id != self._profile:
                raise ValueError('context changed')
            funds = money_view(capture.observations,{})
            self.balance_stale = False
            return dict(checked_at=capture.finished_at.astimezone(KST).strftime('%m/%d %H:%M:%S'),
                order_money=funds.order_money, held=sum(r[1] for r in capture.observations.holdings),
                pending=sum(r[3] for r in capture.observations.unfilled),
                positions=tuple((r[0],r[1]) for r in capture.observations.holdings), ready_for_orders=False)
        except Exception:
            self.close()
            raise UncertainResponse('모의 잔고를 다시 확인하지 못했어요. 연결을 다시 확인해 주세요.') from None

    def start(self):
        if self.state != 'VERIFIED':
            raise ContractError('먼저 모의 계좌를 확인해 주세요.')
        try:
            self._guard(starting=True)
            self.stream = self._stream_factory(self.transport)
            self.stream.open(expected_account=self._account)
            self.state = 'CONNECTING'
        except Exception:
            self.close()
            raise UncertainResponse('알림 받기를 시작하지 못했어요. 계좌부터 다시 확인해 주세요.') from None

    def poll(self):
        if self.state not in ('CONNECTING','RECEIVING'):
            raise ContractError('알림 받기를 먼저 시작해 주세요.')
        try:
            self._guard()
            notices = self.stream.read_once()
            self.state = 'RECEIVING' if self.stream.state == 'RECEIVING' else 'CONNECTING'
            messages = []
            kinds = {'NEW':'새 주문','AMEND':'주문 수정','CANCEL':'주문 취소'}
            statuses = {'접수':'요청을 받았어요','확인':'처리 확인 알림이에요','체결':'주식이 거래됐다는 알림이에요',
                        '취소':'취소 처리 알림이에요','거부':'요청이 거절됐어요'}
            for notice in notices:
                if not self.stream.accepts(notice):
                    raise UncertainResponse('현재 연결의 알림이 아닙니다.')
                self.notice_count += 1
                self.balance_stale = True
                messages.append(f"종목 {notice.code} · {kinds[notice.action]} · {statuses[notice.status]}\n잔고를 다시 확인해야 해요.")
            return messages
        except Exception:
            self.close()
            raise UncertainResponse('연결이 끊겼거나 다시 확인이 필요해요. 자동으로 재연결하지 않아요.') from None

    def close(self):
        self.state, self.balance_stale = 'CLOSED', True
        self._account = None
        try:
            if self.stream is not None:
                self.stream.close()
        finally:
            self.transport.close()
