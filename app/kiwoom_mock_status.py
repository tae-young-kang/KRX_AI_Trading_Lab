"""Offline status/funds presentation. No websocket, GUI, or order execution.

00 events contain time but no trading date: these are session-scoped
observations only, never proof of final cancellation/settlement or permission.
"""
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import hmac
import re

from app.kiwoom_mock_contract import ContractError, UncertainResponse
from app.kiwoom_mock_review import Observations, order_number, stock_code
from app.order_engine import OrderRequest
from app.trading_clock import KST


@dataclass(frozen=True)
class OrderNotice:
    account_tag: str = field(repr=False)
    session: str = field(repr=False)
    received_at: datetime
    order_no: str
    original_no: str | None
    code: str
    side: str
    action: str
    status: str
    auth_generation: str | None = field(default=None, repr=False)
    profile_id: str | None = field(default=None, repr=False)


def order_notice(message, *, expected_account, profile_id, session,
                 session_started_at, received_at, auth_generation=None):
    """Parse one REAL/00 item supplied by the current MOCK socket owner.

    This parser does not authenticate a socket. Caller must own a verified MOCK
    connection; there is deliberately no listener/automatic reconnect here.
    """
    if (not isinstance(expected_account, str) or not re.fullmatch(r'[0-9]{10}', expected_account)
            or not isinstance(profile_id, str) or not re.fullmatch(r'[a-f0-9]{32}', profile_id)
            or not isinstance(session, str) or not re.fullmatch(r'[a-f0-9]{32}', session)):
        raise ContractError('모의 연결 확인 정보가 필요합니다.')
    if auth_generation is not None and (not isinstance(auth_generation, str)
            or not re.fullmatch(r'[a-f0-9]{32}', auth_generation)):
        raise ContractError('인증 연결 확인 정보가 필요합니다.')
    for moment in (session_started_at, received_at):
        if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
            raise ContractError('알림을 받은 한국 시간을 확인하세요.')
    start, received = session_started_at.astimezone(KST), received_at.astimezone(KST)
    if start > received or start.date() != received.date():
        raise UncertainResponse('날짜가 바뀐 연결은 새로 확인해야 합니다.')
    if not isinstance(message, dict) or message.get('type') != '00':
        raise UncertainResponse('주문 처리 알림이 아닙니다.')
    values = message.get('values')
    if not isinstance(values, dict):
        raise UncertainResponse('주문 처리 알림이 불완전합니다.')
    actual = values.get('9201')
    if (not isinstance(actual, str) or not re.fullmatch(r'[0-9]{10}', actual)
            or not hmac.compare_digest(actual, expected_account)):
        raise UncertainResponse('다른 계좌의 알림입니다.')
    if values.get('2134') != '1' or values.get('913') not in ('접수', '체결', '확인', '취소', '거부'):
        raise UncertainResponse('거래소·처리 상태를 확인할 수 없습니다.')
    try:
        if not isinstance(values.get('908'), str) or not re.fullmatch(r'[0-9]{6}', values['908']):
            raise ValueError
        clock = datetime.strptime(values.get('908', ''), '%H%M%S').time()
        event_time = datetime.combine(received.date(), clock, tzinfo=KST)
    except (TypeError, ValueError):
        raise UncertainResponse('알림 시간을 확인할 수 없습니다.') from None
    if event_time < start.replace(microsecond=0) or not -5 <= (received - event_time).total_seconds() <= 120:
        raise UncertainResponse('현재 연결에서 받은 최근 알림이 아닙니다.')
    raw_kind = values.get('905')
    if not isinstance(raw_kind, str):
        raise UncertainResponse('주문 종류를 확인할 수 없습니다.')
    kind = raw_kind[1:] if raw_kind[:1] in ('+', '-') else raw_kind
    kinds = {'매수': ('BUY', 'NEW'), '매도': ('SELL', 'NEW'),
             '매수정정': ('BUY', 'AMEND'), '매도정정': ('SELL', 'AMEND'),
             '매수취소': ('BUY', 'CANCEL'), '매도취소': ('SELL', 'CANCEL')}
    if kind not in kinds:
        raise UncertainResponse('주문 종류를 확인할 수 없습니다.')
    side, action = kinds[kind]
    if values.get('907') != ('2' if side == 'BUY' else '1'):
        raise UncertainResponse('매수·매도 구분이 일치하지 않습니다.')
    tag = hashlib.sha256(('MOCK:' + profile_id + ':' + actual).encode()).hexdigest()
    return OrderNotice(tag, session, received, order_number(values.get('9203')),
        order_number(values.get('904'), root=True), stock_code(values.get('9001')),
        side, action, values['913'], auth_generation, profile_id)


@dataclass(frozen=True)
class SimpleOrderStatus:
    title: str
    detail: str
    remaining_quantity: int
    cancellation_notice_seen: bool
    ready_for_orders: bool = False


def cancellation_status(chain, observed, notice, *, session, now, code, side):
    from app.kiwoom_mock_chain import ChainExecutionView
    if type(chain) is not ChainExecutionView or type(observed) is not Observations:
        raise ContractError('대조한 주문·잔고 기록이 필요합니다.')
    if chain.account_tag != observed.account_tag:
        raise UncertainResponse('주문과 잔고의 모의 계좌가 다릅니다.')
    if (not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None
            or now.astimezone(KST).date() != observed.day or chain.day != observed.day):
        raise ContractError('오늘의 주문 상태를 다시 확인하세요.')
    # A chain itself carries no account tag; notice and snapshot must agree.
    remaining = sum(row[3] for row in observed.unfilled if row[0] in chain.order_nos)
    for number, row_code, _, _ in observed.unfilled:
        if number in chain.order_nos and row_code != code:
            raise UncertainResponse('남은 주문의 종목이 다릅니다.')
    seen = False
    if notice is not None:
        if (type(notice) is not OrderNotice or notice.account_tag != observed.account_tag
                or notice.session != session or notice.received_at.date() != observed.day
                or not 0 <= (now - notice.received_at).total_seconds() <= 120
                or notice.code != code or notice.side != side
                or notice.order_no != chain.order_nos[-1] or notice.action != 'CANCEL'
                or notice.original_no not in chain.order_nos[:-1]):
            raise UncertainResponse('현재 주문과 연결된 최근 취소 알림이 아닙니다.')
        seen = notice.status == '취소'
    if not chain.cancel_request_acknowledged:
        if notice is not None:
            raise UncertainResponse('기록에 없는 취소 요청의 알림입니다.')
        return SimpleOrderStatus('주문 상태 확인', f'조회된 남은 주문은 {remaining:,}주예요.', remaining, False)
    if seen:
        return SimpleOrderStatus('취소 처리 알림 확인',
            f'조회된 남은 주문은 {remaining:,}주예요. 알림 뒤의 최종 수량은 다시 확인해야 해요.', remaining, True)
    if notice is not None and notice.status == '거부':
        return SimpleOrderStatus('취소 요청이 거절됐어요',
            f'조회된 남은 주문은 {remaining:,}주예요. 주문 상태를 다시 확인해 주세요.', remaining, False)
    return SimpleOrderStatus('취소 확인 중',
        f'취소 요청을 보냈어요. 조회된 남은 주문은 {remaining:,}주예요.', remaining, False)


@dataclass(frozen=True)
class MoneyView:
    order_money: int | None
    deposit: int
    tomorrow_deposit: int
    second_day_deposit: int
    unfilled_buy_estimate: int | None
    sell_reserved: tuple[tuple[str, int], ...]
    expected_values_match: bool | None
    messages: tuple[str, ...]
    order_money_label: str = '지금 주문에 쓸 수 있는 돈(조회값)'
    reserve_label: str = '아직 안 산 주문금액(수수료 별도)'
    ready_for_orders: bool = False


def money_view(observed, known_orders, *, expected_deposit=None):
    """known_orders is an independently mapped broker-number -> OrderRequest.

    Available money already reflects broker rules/reservations. NEVER subtract
    our gross estimate again. Expected balances must come from independent
    evidence, not the synthetic engine's gross cash or summed fee observations.
    """
    if type(observed) is not Observations or not isinstance(known_orders, dict):
        raise ContractError('모의 계좌와 주문 기록이 필요합니다.')
    cash = dict(observed.deposit)
    fields = ('entr', 'ord_alow_amt', '100stk_ord_alow_amt', 'd1_entra', 'd2_entra')
    if len(observed.deposit) != len(fields) or set(cash) != set(fields) or any(type(cash[key]) is not int for key in fields):
        raise ContractError('돈의 상태를 확인할 조회값이 불완전합니다.')
    messages, reserved, sold, seen = [], 0, {}, set()
    complete = True
    for number, code, quantity, remaining in observed.unfilled:
        req = known_orders.get(number)
        if (type(req) is not OrderRequest or req.code != code or req.venue != 'KRX'
                or number in seen or req.quantity != quantity
                or type(quantity) is not int or type(remaining) is not int or not 0 < remaining <= quantity):
            complete = False
            continue
        seen.add(number)
        req.validate()
        if req.side == 'BUY':
            reserved += remaining * req.limit_price
        else:
            sold[code] = sold.get(code, 0) + remaining
    held = {}
    for code, quantity, available in observed.holdings:
        if (code in held or type(quantity) is not int or type(available) is not int
                or not 0 <= available <= quantity):
            raise ContractError('보유 수량을 다시 확인해야 해요.')
        held[code] = quantity
    if any(quantity > held.get(code, 0) for code, quantity in sold.items()):
        complete = False
    if not complete:
        messages.append('기록과 맞지 않는 주문이 있어요. 주문 내용을 다시 확인해 주세요.')
    usable = min(cash['ord_alow_amt'], cash['100stk_ord_alow_amt'])
    if usable < 0 or not complete:
        usable = None
        messages.append('주문에 쓸 수 있는 돈을 다시 확인해야 해요.')
    matched = None
    if expected_deposit is not None:
        if (not isinstance(expected_deposit, dict) or set(expected_deposit) != set(fields)
                or any(type(value) is not int for value in expected_deposit.values())):
            raise ContractError('별도로 확인한 돈의 기록이 필요합니다.')
        matched = cash == expected_deposit
        if not matched:
            messages.append('조회한 돈과 확인해 둔 기록이 달라요.')
            usable = None
    messages.append('내일·모레 들어올 돈은 지금 쓸 수 있는 돈에 더하지 않아요.')
    return MoneyView(usable, cash['entr'], cash['d1_entra'], cash['d2_entra'],
                     reserved if complete else None, tuple(sorted(sold.items())) if complete else (), matched, tuple(messages))
