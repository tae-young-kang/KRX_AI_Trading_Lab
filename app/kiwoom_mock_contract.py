"""Offline Kiwoom MOCK wire contract. No transport, tokens, or order execution.

An acknowledgement is NOT a BrokerOrder/fill. The offline engine deliberately
cannot accept this module as a broker. See docs/KIWOOM_MOCK_PREPARATION.md.
"""
import re
from datetime import date
from dataclasses import dataclass

from app.order_engine import OrderRequest, OrderError


class ContractError(OrderError):
    pass


class UncertainResponse(ContractError):
    """Do not retry a mutation after this result."""


class RequestRefused(ContractError):
    """Structured nonzero business result; raw broker text is never exposed."""


@dataclass(frozen=True)
class MockPlan:
    api_id: str
    fields: tuple[tuple[str, str], ...]

    @property
    def url(self):
        path = 'ordr' if self.api_id.startswith('kt100') else 'acnt'
        return 'https://mockapi.kiwoom.com/api/dostk/' + path

    def body(self):
        return dict(self.fields)


@dataclass(frozen=True)
class Acknowledgement:
    order_no: str
    base_order_no: str | None
    # This means request accepted only, including amend/cancel requests.
    state: str = 'ACKNOWLEDGED'


def _number(value, *, zero=False):
    if type(value) is not int or value < (0 if zero else 1) or value >= 10**12:
        raise ContractError('수량과 가격은 허용 범위의 정수여야 합니다.')
    return str(value)


def _order_no(value):
    # Official examples include 5 digits although field length is 7.
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,7}', value) or int(value) == 0:
        raise ContractError('주문번호 형식을 확인하세요.')
    return value  # Preserve leading zeros; never infer a client reference.


def _code(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9]{6}', value):
        raise ContractError('6자리 종목코드를 확인하세요.')
    return value


def order_plan(request):
    if not isinstance(request, OrderRequest):
        raise ContractError('주문 내용을 확인하세요.')
    request.validate()
    if request.venue != 'KRX':
        raise ContractError('준비 중인 모의 연결은 KRX 지정가만 지원합니다.')
    return MockPlan('kt10000' if request.side == 'BUY' else 'kt10001', (
        ('dmst_stex_tp', 'KRX'), ('stk_cd', _code(request.code)),
        ('ord_qty', _number(request.quantity)), ('ord_uv', _number(request.limit_price)),
        ('trde_tp', '0'), ('cond_uv', ''),
    ))


def market_order_plan(*,code,side,quantity,venue='KRX'):
    """Official market-order shape, prepared offline only.

    No placeholder limit price. MockTransport intentionally does not accept
    this shape until market-specific funds and confirmation gates are wired.
    """
    if side not in ('BUY','SELL') or venue!='KRX':
        raise ContractError('시장가 계약 준비는 KRX 매수·매도만 지원합니다.')
    return MockPlan('kt10000' if side=='BUY' else 'kt10001',(
        ('dmst_stex_tp','KRX'),('stk_cd',_code(code)),
        ('ord_qty',_number(quantity)),('trde_tp','3'),
    ))


def amend_plan(code, original_order_no, *, remaining_quantity, limit_price):
    """Explicit broker remaining quantity, NEVER engine's total quantity.

    Zero means all remaining per Kiwoom. A future bridge must reconcile races
    with fills before translating the engine's total-quantity amend semantics.
    """
    return MockPlan('kt10002', (
        ('dmst_stex_tp', 'KRX'), ('orig_ord_no', _order_no(original_order_no)),
        ('stk_cd', _code(code)), ('mdfy_qty', _number(remaining_quantity, zero=True)),
        ('mdfy_uv', _number(limit_price)), ('mdfy_cond_uv', ''),
    ))


def cancel_plan(code, original_order_no, *, remaining_quantity=0):
    return MockPlan('kt10003', (
        ('dmst_stex_tp', 'KRX'), ('orig_ord_no', _order_no(original_order_no)),
        ('stk_cd', _code(code)), ('cncl_qty', _number(remaining_quantity, zero=True)),
    ))


def query_plan(kind, *, trade_date=None):
    choices = {
        'unfilled': ('ka10075', {'all_stk_tp': '0', 'trde_tp': '0', 'stk_cd': '', 'stex_tp': '1'}),
        # ord_no is a historical cursor, NOT an exact order-number filter.
        'fills': ('ka10076', {'stk_cd': '', 'qry_tp': '0', 'sell_tp': '0', 'ord_no': '', 'stex_tp': '1'}),
        'holdings': ('kt00018', {'qry_tp': '2', 'dmst_stex_tp': 'KRX'}),
        'deposit': ('kt00001', {'qry_tp': '2'}),
        'identity': ('ka00001', {}),
    }
    if kind == 'executions':
        if type(trade_date) is not date:
            raise ContractError('체결 조회 날짜가 필요합니다.')
        return MockPlan('kt00009', tuple(dict(ord_dt=trade_date.strftime('%Y%m%d'),
            stk_bond_tp='1', mrkt_tp='0', sell_tp='0', qry_tp='1', stk_cd='',
            fr_ord_no='', dmst_stex_tp='KRX').items()))
    if trade_date is not None:
        raise ContractError('이 조회에는 날짜를 지정할 수 없습니다.')
    if kind not in choices:
        raise ContractError('지원하지 않는 조회입니다.')
    api_id, body = choices[kind]
    return MockPlan(api_id, tuple(body.items()))


def _success(payload):
    if not isinstance(payload, dict) or type(payload.get('return_code')) is not int:
        raise UncertainResponse('응답을 확인할 수 없습니다. 재전송하지 말고 조회하세요.')
    if payload['return_code'] != 0:
        raise RequestRefused('증권사가 요청을 거절했습니다. 주문 상태를 조회하세요.')


def acknowledgement(api_id, payload):
    if api_id not in ('kt10000', 'kt10001', 'kt10002', 'kt10003'):
        raise ContractError('주문 응답 종류를 확인하세요.')
    if (isinstance(payload, dict) and type(payload.get('return_code')) is int
            and payload['return_code'] != 0 and payload.get('ord_no')):
        raise UncertainResponse('거절 응답에 주문번호가 있습니다. 주문 상태를 조회하세요.')
    _success(payload)
    try:
        order_no = _order_no(payload.get('ord_no'))
        base = (_order_no(payload.get('base_orig_ord_no'))
                if api_id in ('kt10002', 'kt10003') else None)
        if payload.get('dmst_stex_tp', 'KRX') != 'KRX':
            raise ContractError('거래소 불일치')
    except ContractError:
        raise UncertainResponse('접수 번호가 불확실합니다. 재전송하지 말고 조회하세요.') from None
    return Acknowledgement(order_no, base)


def query_page(kind, payload, headers):
    """Whitelisted rows only; never forward account numbers or broker messages.

    Returns a single page and cursor. Missing list/continuation metadata is NOT
    an empty complete account. No cumulative fill/state interpretation yet.
    """
    keys = {'unfilled': ('oso', ('ord_no', 'orig_ord_no', 'stk_cd', 'ord_qty', 'oso_qty')),
            'fills': ('cntr', ('ord_no', 'orig_ord_no', 'stk_cd', 'cntr_qty', 'cntr_pric',
                               'tdy_trde_cmsn', 'tdy_trde_tax')),
            'holdings': ('acnt_evlt_remn_indv_tot', ('stk_cd', 'rmnd_qty', 'trde_able_qty')),
            'executions': ('acnt_ord_cntr_prst_array', ('ord_no', 'orig_ord_no', 'cntr_no',
                'stk_cd', 'io_tp_nm', 'cntr_qty', 'cntr_uv', 'dmst_stex_tp'))}
    if kind not in keys:
        raise ContractError('지원하지 않는 목록입니다.')
    _success(payload)
    header = {str(k).lower(): v for k, v in headers.items()}
    continuation = header.get('cont-yn')
    cursor = header.get('next-key', '')
    if continuation not in ('Y', 'N') or not isinstance(cursor, str) or (continuation == 'Y' and not cursor.strip()):
        raise UncertainResponse('연속 조회 정보를 확인할 수 없습니다.')
    key, allowed = keys[kind]
    rows = payload.get(key)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise UncertainResponse('조회 목록이 불완전합니다.')
    result = []
    for row in rows:
        if any(not isinstance(row.get(field), str) for field in allowed):
            raise UncertainResponse('조회 항목이 불완전합니다.')
        result.append({field: row[field] for field in allowed})
    return result, cursor if continuation == 'Y' else None
