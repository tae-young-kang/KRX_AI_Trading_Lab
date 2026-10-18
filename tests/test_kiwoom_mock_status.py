"""Plain Korean status and funds math on synthetic evidence only."""
from dataclasses import replace
from datetime import datetime, timedelta
import hashlib

import pytest

from app.kiwoom_mock_chain import ChainExecutionView
from app.kiwoom_mock_status import order_notice, cancellation_status, money_view
from app.kiwoom_mock_contract import ContractError, UncertainResponse
from app.order_engine import OrderRequest
from app.trading_clock import KST
from test_kiwoom_mock_ledger import snapshot, DAY

ACCOUNT, PROFILE, SESSION = '0' * 10, 'a' * 32, 'b' * 32
TAG = hashlib.sha256(('MOCK:' + PROFILE + ':' + ACCOUNT).encode()).hexdigest()
START = datetime(2026, 10, 19, 10, tzinfo=KST)
NOW = START + timedelta(seconds=10)


def event(**changes):
    values = {'9201': ACCOUNT, '9203': '13', '904': '12', '9001': 'A900001',
        '905': '+매수취소', '907': '2', '913': '취소', '2134': '1', '908': '100010'}
    values.update(changes)
    return {'type': '00', 'values': values}


def parse(message=None):
    return order_notice(message or event(), expected_account=ACCOUNT, profile_id=PROFILE,
        session=SESSION, session_started_at=START, received_at=NOW)


def observed(*, pending=True):
    return replace(snapshot(), account_tag=TAG, holdings=(('900001', 3, 1),),
        unfilled=(('0000012', '900001', 3, 2),) if pending else ())


def chain():
    return ChainExecutionView(DAY, 'buy', ('0000012', '0000013'), 1, 1000,
                               None, None, None, None, True, TAG)


def status(notice=None, **changes):
    return cancellation_status(chain(), observed(), notice, session=SESSION, now=NOW,
                               code='900001', side='BUY', **changes)


def test_plain_cancel_status_never_claims_whole_order_complete():
    pending = status()
    assert pending.title == '취소 확인 중' and pending.remaining_quantity == 2
    assert not pending.cancellation_notice_seen and not pending.ready_for_orders
    confirmed_notice = status(parse())
    assert confirmed_notice.title == '취소 처리 알림 확인'
    assert confirmed_notice.remaining_quantity == 2 and confirmed_notice.cancellation_notice_seen
    assert '완료' not in confirmed_notice.title and not confirmed_notice.ready_for_orders
    empty = cancellation_status(chain(), observed(pending=False), None,
        session=SESSION, now=NOW, code='900001', side='BUY')
    assert empty.title == '취소 확인 중'  # Missing from unfilled is not cancellation proof.
    assert empty.remaining_quantity == 0


@pytest.mark.parametrize('changes', [{'9201': '9' * 10}, {'9201': '계좌'}, {'2134': '2'},
    {'913': '모르는 상태'}, {'907': '1'}, {'905': None}, {'905': '++매수취소'},
    {'908': '095959'}, {'908': '10001'}, {'908': '235959'}])
def test_wrong_account_market_time_and_kind_are_blocked(changes):
    with pytest.raises(UncertainResponse):
        parse(event(**changes))


def test_notice_has_no_account_value_in_repr_and_is_session_scoped():
    notice = parse()
    assert ACCOUNT not in repr(notice)
    for invalid in (replace(notice, session='c' * 32),
                    replace(notice, account_tag='d' * 64),
                    replace(notice, original_no='0000099'),
                    replace(notice, received_at=NOW-timedelta(seconds=121))):
        with pytest.raises(UncertainResponse):
            status(invalid)
    with pytest.raises(UncertainResponse):
        cancellation_status(replace(chain(), account_tag='d' * 64), observed(), notice,
            session=SESSION, now=NOW, code='900001', side='BUY')


def test_midnight_session_and_naive_time_are_not_accepted():
    with pytest.raises(UncertainResponse):
        order_notice(event(), expected_account=ACCOUNT, profile_id=PROFILE,
            session=SESSION, session_started_at=START-timedelta(days=1), received_at=NOW)
    with pytest.raises(ContractError):
        order_notice(event(), expected_account=ACCOUNT, profile_id=PROFILE,
            session=SESSION, session_started_at=START.replace(tzinfo=None), received_at=NOW)


def test_broker_money_not_reduced_twice_or_increased_by_future_deposit():
    view = money_view(observed(), {'0000012': OrderRequest('900001', 'BUY', 3, 1000)},
                      expected_deposit=dict(observed().deposit))
    assert view.order_money == 97010  # Not 95010 after another local reservation deduction.
    assert view.unfilled_buy_estimate == 2000
    assert view.tomorrow_deposit == 100000 and view.expected_values_match is True
    assert not view.ready_for_orders
    assert view.order_money_label == '지금 주문에 쓸 수 있는 돈(조회값)'
    assert view.reserve_label == '아직 안 산 주문금액(수수료 별도)'


def test_sell_reservation_and_balance_difference_are_separate():
    view = money_view(observed(), {'0000012': OrderRequest('900001', 'SELL', 3, 1000)},
        expected_deposit={**dict(observed().deposit), 'd2_entra': 123})
    assert view.sell_reserved == (('900001', 2),)
    assert view.unfilled_buy_estimate == 0
    assert view.expected_values_match is False
    assert any('기록이 달라요' in text for text in view.messages)


def test_unknown_duplicate_or_mismatched_order_does_not_show_spendable_money():
    for known in ({}, {'0000012': OrderRequest('900002', 'BUY', 3, 1000)},
                  {'0000012': OrderRequest('900001', 'BUY', 2, 1000)}):
        view = money_view(observed(), known)
        assert view.order_money is None and view.unfilled_buy_estimate is None
    duplicate = replace(observed(), unfilled=observed().unfilled * 2)
    assert money_view(duplicate, {'0000012': OrderRequest('900001', 'BUY', 3, 1000)}).order_money is None


def test_missing_cash_is_not_zero_and_negative_available_is_unknown():
    with pytest.raises(ContractError):
        money_view(replace(observed(), deposit=observed().deposit[:-1]), {})
    negative = replace(observed(pending=False), deposit=tuple((key, -1 if key == 'ord_alow_amt' else value)
                                                            for key, value in observed().deposit))
    assert money_view(negative, {}).order_money is None
