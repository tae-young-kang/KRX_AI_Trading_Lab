"""Wire fixtures only: no HTTP, credentials, GUI, or broker execution."""
import pytest

from app.kiwoom_mock_contract import (
    ContractError, UncertainResponse, RequestRefused, order_plan, amend_plan,
    cancel_plan, query_plan, acknowledgement, query_page,
)
from app.order_engine import OrderRequest


@pytest.mark.parametrize('side,api', [('BUY', 'kt10000'), ('SELL', 'kt10001')])
def test_limit_order(side, api):
    plan = order_plan(OrderRequest('900001', side, 2, 1000))
    assert plan.api_id == api
    assert plan.url == 'https://mockapi.kiwoom.com/api/dostk/ordr'
    assert plan.body() == dict(dmst_stex_tp='KRX', stk_cd='900001', ord_qty='2',
                               ord_uv='1000', trde_tp='0', cond_uv='')
    plan.body()['ord_qty'] = '999'
    assert plan.body()['ord_qty'] == '2'


@pytest.mark.parametrize('qty', [True, -1, 1.5, 10**12])
def test_invalid_quantity(qty):
    with pytest.raises(ContractError):
        cancel_plan('900001', '0000012', remaining_quantity=qty)


def test_amend_cancel_ack_is_not_fill_or_cancel_completion():
    plan = amend_plan('900001', '0000012', remaining_quantity=0, limit_price=1000)
    assert plan.body()['mdfy_qty'] == '0'
    assert cancel_plan('900001', '0000013').body()['cncl_qty'] == '0'
    result = acknowledgement('kt10003', dict(return_code=0, ord_no='0000014',
                                             base_orig_ord_no='0000012'))
    assert result.order_no == '0000014' and result.base_order_no == '0000012'
    assert result.state == 'ACKNOWLEDGED'


@pytest.mark.parametrize('payload', [{}, {'return_code': False}, {'return_code': '0'},
                                      {'return_code': 0},
                                      {'return_code': 0, 'ord_no': '0'},
                                      {'return_code': 0, 'ord_no': '0000012', 'dmst_stex_tp': 'NXT'}])
def test_uncertain_response(payload):
    with pytest.raises(UncertainResponse):
        acknowledgement('kt10000', payload)


def test_refusal_redacts_response_text():
    with pytest.raises(RequestRefused) as error:
        acknowledgement('kt10000', {'return_code': 10, 'return_msg': 'PRIVATE_RESPONSE'})
    assert 'PRIVATE_RESPONSE' not in str(error.value)


def test_market_and_query_separation():
    with pytest.raises(ContractError):
        order_plan(OrderRequest('900001', 'BUY', 1, 1000, 'NXT'))
    assert query_plan('fills').body()['ord_no'] == ''
    for kind in ('unfilled', 'fills', 'holdings', 'deposit'):
        assert query_plan(kind).url == 'https://mockapi.kiwoom.com/api/dostk/acnt'
    with pytest.raises(ContractError):
        query_plan('live')


def test_page_whitelist_continuation_and_incomplete_are_distinct():
    row = dict(ord_no='0000012', orig_ord_no='0000000', stk_cd='900001',
               ord_qty='00002', oso_qty='00001', acnt_no='PRIVATE_ACCOUNT')
    rows, cursor = query_page('unfilled', dict(return_code=0, oso=[row]),
                              {'Cont-Yn': 'Y', 'Next-Key': 'fixture-page-two'})
    assert cursor == 'fixture-page-two' and 'acnt_no' not in rows[0]
    assert rows[0]['ord_no'] == '0000012'
    assert query_page('unfilled', dict(return_code=0, oso=[]), {'cont-yn': 'N'}) == ([], None)
    for payload, headers in [(dict(return_code=0), {'cont-yn': 'N'}),
                             (dict(return_code=0, oso=[]), {}),
                             (dict(return_code=0, oso=[]), {'cont-yn': 'Y'}),
                             (dict(return_code=0, oso=[{}]), {'cont-yn': 'N'})]:
        with pytest.raises(UncertainResponse):
            query_page('unfilled', payload, headers)
