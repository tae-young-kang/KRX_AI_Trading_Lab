"""Read-only MOCK observations. Never clear UNKNOWN or authorize execution.

Repeated observations are not an atomic broker snapshot. Fees from ka10076
are observations, not additive per-fill fees or proof of final settlement.
"""
from dataclasses import dataclass, field
from datetime import date
import hashlib
import hmac
import re

from app.kiwoom_mock_contract import ContractError, UncertainResponse, query_plan, _success
from app.kiwoom_mock_transport import MockTransport


def integer(value, *, signed=False):
    pattern = r'[+-]?[0-9]{1,16}' if signed else r'\+?[0-9]{1,16}'
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise UncertainResponse('수량·금액 형식이 불완전합니다.')
    return int(value)


def stock_code(value):
    if isinstance(value, str) and len(value) == 7 and value[0] in 'AQ':
        value = value[1:]
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9]{6}', value):
        raise UncertainResponse('지원하지 않는 보유 종목 형식입니다.')
    return value


def order_number(value, *, root=False):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,7}', value):
        raise UncertainResponse('주문·체결 번호가 불완전합니다.')
    if int(value) == 0:
        if root:
            return None
        raise UncertainResponse('주문·체결 번호가 비어 있습니다.')
    return value.zfill(7)


@dataclass(frozen=True, order=True)
class Execution:
    order_no: str
    fill_no: str
    original_no: str | None
    code: str
    side: str
    quantity: int
    price: int

    @property
    def gross(self):
        return self.quantity * self.price


@dataclass(frozen=True, order=True)
class FeeObservation:
    order_no: str
    code: str
    commission: int
    tax: int


@dataclass(frozen=True)
class Observations:
    day: date
    account_tag: str = field(repr=False)
    # Cash concepts stay separate: no gross-engine cash / settlement conflation.
    deposit: tuple[tuple[str, int], ...]
    holdings: tuple[tuple[str, int, int], ...]  # code, held, sellable
    executions: tuple[Execution, ...]
    fees: tuple[FeeObservation, ...]
    unfilled: tuple[tuple[str, str, int, int], ...]  # order, code, quantity, remaining


def executions(rows):
    unique = {}
    sides = {'현금매수': 'BUY', '+매수': 'BUY', '매수': 'BUY',
             '현금매도': 'SELL', '-매도': 'SELL', '매도': 'SELL'}
    for row in rows:
        if row['dmst_stex_tp'] != 'KRX' or row['io_tp_nm'] not in sides:
            raise UncertainResponse('거래소·매매 구분을 확인할 수 없습니다.')
        item = Execution(order_number(row['ord_no']), order_number(row['cntr_no']),
            order_number(row['orig_ord_no'], root=True), stock_code(row['stk_cd']),
            sides[row['io_tp_nm']], integer(row['cntr_qty']), integer(row['cntr_uv']))
        if item.quantity <= 0 or item.price <= 0:
            raise UncertainResponse('체결 수량·가격이 유효하지 않습니다.')
        key = item.order_no, item.fill_no
        if key in unique and unique[key] != item:
            raise UncertainResponse('같은 체결번호의 내용이 다릅니다.')
        unique[key] = item
    return tuple(sorted(unique.values()))


def holdings(rows):
    unique = {}
    for row in rows:
        code = stock_code(row['stk_cd'])
        quantity, available = integer(row['rmnd_qty']), integer(row['trde_able_qty'])
        if code in unique or available > quantity:
            raise UncertainResponse('보유·매도 가능 수량이 일치하지 않습니다.')
        unique[code] = code, quantity, available
    return tuple(sorted(unique.values()))


class MockAccountReviewer:
    def __init__(self, transport):
        if type(transport) is not MockTransport or transport.environment != 'MOCK':
            raise ContractError('모의 조회 연결이 필요합니다.')
        self.transport = transport

    def _identity(self, expected_account):
        payload, _ = self.transport.exchange(query_plan('identity'))
        _success(payload)
        actual = payload.get('acctNo')
        if (not isinstance(actual, str) or not re.fullmatch(r'[0-9]{10}', actual)
                or not hmac.compare_digest(actual, expected_account)):
            raise UncertainResponse('예정한 모의 계좌와 인증 계좌가 일치하지 않습니다.')
        # Raw account number is never retained in returned observations/errors.
        return hashlib.sha256(('MOCK:' + self.transport.profile_id + ':' + actual).encode()).hexdigest()

    def _observe(self, day, account_tag):
        payload, _ = self.transport.exchange(query_plan('deposit'))
        _success(payload)
        fields = ('entr', 'ord_alow_amt', '100stk_ord_alow_amt', 'd1_entra', 'd2_entra')
        deposit = tuple((key, integer(payload.get(key), signed=True)) for key in fields)
        held = holdings(self.transport.read_all('holdings'))
        filled = executions(self.transport.read_all('executions', trade_date=day))
        costs = tuple(sorted(set(FeeObservation(order_number(row['ord_no']), stock_code(row['stk_cd']),
            integer(row['tdy_trde_cmsn']), integer(row['tdy_trde_tax']))
            for row in self.transport.read_all('fills'))))
        pending = {}
        for row in self.transport.read_all('unfilled'):
            number = order_number(row['ord_no'])
            value = (number, stock_code(row['stk_cd']), integer(row['ord_qty']), integer(row['oso_qty']))
            if number in pending or not 0 < value[3] <= value[2]:
                raise UncertainResponse('미체결 수량·주문번호가 불완전합니다.')
            pending[number] = value
        return Observations(day, account_tag, deposit, held, filled, costs, tuple(sorted(pending.values())))

    def collect(self, *, expected_account, trade_date):
        """Caller supplies expected MOCK account privately; never infer it.

        ka10076 has no date input, so only today's KST snapshot is supported.
        Two identical normalized passes + same login/day/account are required.
        """
        if (not isinstance(expected_account, str) or not re.fullmatch(r'[0-9]{10}', expected_account)
                or type(trade_date) is not date or trade_date != self.transport.current_day):
            raise ContractError('모의 계좌와 오늘의 한국 날짜를 확인하세요.')
        generation, profile = self.transport.auth_generation, self.transport.profile_id
        tag = self._identity(expected_account)
        first = self._observe(trade_date, tag)
        second = self._observe(trade_date, tag)
        final_tag = self._identity(expected_account)
        if (first != second or tag != final_tag or generation != self.transport.auth_generation
                or profile != self.transport.profile_id or trade_date != self.transport.current_day):
            raise UncertainResponse('조회 도중 계좌·인증·잔고·체결이 바뀌었습니다. 다시 조회하세요.')
        return second

    def collect_into(self, ledger, *, expected_account, trade_date):
        """Only complete/stable observations may update the dedicated ledger."""
        from app.kiwoom_mock_ledger import MockFillLedger
        if type(ledger) is not MockFillLedger:
            raise ContractError('모의 체결 장부가 필요합니다.')
        observed = self.collect(expected_account=expected_account, trade_date=trade_date)
        ledger.ingest(observed)
        return observed


@dataclass(frozen=True)
class Comparison:
    observations_match: bool
    differences: tuple[str, ...]
    open_order_count: int
    buy_gross: int
    sell_gross: int
    # These observations cannot determine net cash flow/settlement/order safety.
    net_cash_flow: None = None
    ready_for_orders: bool = False


def compare(observed, expected):
    """Expected values must come from an independent ledger, never observed itself.

    Matching query observations never clears journal UNKNOWN or opens trading.
    """
    if type(observed) is not Observations or type(expected) is not Observations:
        raise ContractError('대조할 조회 기록이 필요합니다.')
    labels = {'day': '조회 날짜', 'account_tag': '모의 계좌', 'deposit': '예수금·주문가능금액·결제예정금',
              'holdings': '보유·매도가능수량', 'executions': '체결번호·수량·가격',
              'fees': '수수료·세금 조회값', 'unfilled': '미체결 주문'}
    differences = tuple(label for name, label in labels.items() if getattr(observed, name) != getattr(expected, name))
    return Comparison(not differences, differences, len(observed.unfilled),
        sum(item.gross for item in observed.executions if item.side == 'BUY'),
        sum(item.gross for item in observed.executions if item.side == 'SELL'))
