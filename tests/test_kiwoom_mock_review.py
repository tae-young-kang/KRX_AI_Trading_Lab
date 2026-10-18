"""Temporary/fake observations only. No server, account, credential, or GUI."""
from dataclasses import replace
from datetime import date, datetime

import pytest

from app.kiwoom_mock_contract import ContractError, UncertainResponse, query_plan
from app.kiwoom_mock_review import (
    MockAccountReviewer, Observations, Execution, FeeObservation, compare,
    executions, holdings, integer,
)
from app.trading_clock import KST
from test_kiwoom_mock_dispatch import transport, Response

DAY = date(2026, 10, 19)
ACCOUNT = '0' * 10  # Synthetic placeholder; never a real account.


def wire():
    execution = dict(ord_no='0000012', orig_ord_no='0000000', cntr_no='0000001',
        stk_cd='A900001', io_tp_nm='현금매수', cntr_qty='1', cntr_uv='1000', dmst_stex_tp='KRX')
    later = dict(execution, cntr_no='0000002', cntr_qty='2', cntr_uv='990')
    fee = dict(ord_no='0000012', orig_ord_no='0000000', stk_cd='900001', cntr_qty='1',
        cntr_pric='1000', tdy_trde_cmsn='10', tdy_trde_tax='0')
    return [
        dict(return_code=0, entr='100000', ord_alow_amt='97010',
             **{'100stk_ord_alow_amt': '97010'}, d1_entra='100000', d2_entra='97010'),
        dict(return_code=0, acnt_evlt_remn_indv_tot=[dict(stk_cd='A900001', rmnd_qty='00003', trde_able_qty='2')]),
        dict(return_code=0, acnt_ord_cntr_prst_array=[later, execution, execution]),
        dict(return_code=0, cntr=[fee, fee]),
        dict(return_code=0, oso=[]),
    ]


def rig(first=None, second=None, final_identity=None):
    client, http, _ = transport()
    identity = dict(return_code=0, acctNo=ACCOUNT)
    responses = [identity] + (first or wire()) + (second or wire()) + [final_identity or identity]
    http.responses.extend(Response(item, headers={'cont-yn': 'N'}) for item in responses)
    return client, http, MockAccountReviewer(client)


def collect(reviewer):
    return reviewer.collect(expected_account=ACCOUNT, trade_date=DAY)


def test_stable_account_partial_fills_dedup_costs_and_cash_separation():
    client, http, reviewer = rig()
    result = collect(reviewer)
    assert result.day == DAY and ACCOUNT not in repr(result)
    assert result.holdings == (('900001', 3, 2),)
    assert len(result.executions) == 2
    assert sum(item.quantity for item in result.executions) == 3
    assert sum(item.gross for item in result.executions) == 2980
    assert result.fees == (FeeObservation('0000012', '900001', 10, 0),)
    assert dict(result.deposit) == dict(entr=100000, ord_alow_amt=97010,
        **{'100stk_ord_alow_amt': 97010}, d1_entra=100000, d2_entra=97010)
    assert all(options['headers'].get('api-id') not in ('kt10000', 'kt10001', 'kt10002', 'kt10003')
               for _, options in http.calls)
    assert all('mockapi.kiwoom.com' in url for url, _ in http.calls)
    assert query_plan('executions', trade_date=DAY).body()['ord_dt'] == '20261019'


def test_account_mismatch_never_exposes_number():
    client, http, _ = transport()
    http.responses.append(Response(dict(return_code=0, acctNo='9' * 10)))
    with pytest.raises(UncertainResponse) as error:
        collect(MockAccountReviewer(client))
    assert '9' * 10 not in str(error.value) and ACCOUNT not in str(error.value)
    assert len(http.calls) == 2


@pytest.mark.parametrize('field', ['entr', 'ord_alow_amt', '100stk_ord_alow_amt', 'd1_entra', 'd2_entra'])
def test_missing_money_is_not_zero(field):
    first = wire()
    del first[0][field]
    _, _, reviewer = rig(first=first)
    with pytest.raises(UncertainResponse):
        collect(reviewer)


def test_changing_snapshot_or_account_is_not_consistent():
    second = wire()
    second[1]['acnt_evlt_remn_indv_tot'][0]['rmnd_qty'] = '4'
    _, _, reviewer = rig(second=second)
    with pytest.raises(UncertainResponse):
        collect(reviewer)
    _, _, reviewer = rig(final_identity=dict(return_code=0, acctNo='9' * 10))
    with pytest.raises(UncertainResponse):
        collect(reviewer)


@pytest.mark.parametrize('value', [None, '', '-1', '1.0', '1,000', True, '１', '1e3'])
def test_strict_quantities(value):
    with pytest.raises(UncertainResponse):
        integer(value)
    assert integer('+0003') == 3 and integer('-0003', signed=True) == -3


def test_conflicting_or_unidentified_execution_fails_closed():
    rows = wire()[2]['acnt_ord_cntr_prst_array']
    assert executions(rows) == executions(list(reversed(rows)))
    conflict = [dict(row) for row in rows]
    conflict[-1]['cntr_uv'] = '1001'
    with pytest.raises(UncertainResponse):
        executions(conflict)
    for field, value in [('cntr_no', '0000000'), ('cntr_qty', '0'), ('cntr_uv', '-1000'),
                         ('dmst_stex_tp', 'NXT'), ('io_tp_nm', '신용매수')]:
        with pytest.raises(UncertainResponse):
            executions([dict(rows[0], **{field: value})])


def test_holdings_and_pending_are_validated():
    with pytest.raises(UncertainResponse):
        holdings([dict(stk_cd='A900001', rmnd_qty='2', trde_able_qty='3')])
    row = dict(stk_cd='A900001', rmnd_qty='2', trde_able_qty='1')
    with pytest.raises(UncertainResponse):
        holdings([row, row])
    first = wire()
    first[-1]['oso'] = [dict(ord_no='0000012', orig_ord_no='0000000', stk_cd='900001', ord_qty='2', oso_qty='3')]
    _, _, reviewer = rig(first=first)
    with pytest.raises(UncertainResponse):
        collect(reviewer)


def test_day_rollover_and_login_change_invalidate_collection():
    client, http, reviewer = rig()
    original_post = http.post
    count = 0

    def rollover(url, **kwargs):
        nonlocal count
        result = original_post(url, **kwargs)
        count += 1
        if count == 12:
            client._clock = lambda: datetime(2026, 10, 20, 0, tzinfo=KST)
        return result

    http.post = rollover
    with pytest.raises(UncertainResponse):
        collect(reviewer)
    client, http, reviewer = rig()
    original_post = http.post

    def changed_auth(url, **kwargs):
        result = original_post(url, **kwargs)
        client._auth_generation = 'different-login'
        return result

    http.post = changed_auth
    with pytest.raises(UncertainResponse):
        collect(reviewer)


def test_historical_date_cannot_mix_today_fees():
    _, http, reviewer = rig()
    with pytest.raises(ContractError):
        reviewer.collect(expected_account=ACCOUNT, trade_date=date(2026, 10, 18))
    assert len(http.calls) == 1


def test_comparison_reports_specific_differences_without_enabling_orders():
    _, _, reviewer = rig()
    observed = collect(reviewer)
    # Independent expected fixture: never computed by copying observed balances.
    expected = Observations(DAY, observed.account_tag,
        (('entr', 100000), ('ord_alow_amt', 97010), ('100stk_ord_alow_amt', 97010),
         ('d1_entra', 100000), ('d2_entra', 97010)), (('900001', 3, 2),),
        (Execution('0000012', '0000001', None, '900001', 'BUY', 1, 1000),
         Execution('0000012', '0000002', None, '900001', 'BUY', 2, 990)),
        (FeeObservation('0000012', '900001', 10, 0),), ())
    report = compare(observed, expected)
    assert report.observations_match and report.buy_gross == 2980
    assert report.net_cash_flow is None and report.ready_for_orders is False
    bad = replace(expected, fees=(), holdings=(), account_tag='another-account')
    report = compare(observed, bad)
    assert not report.observations_match
    assert set(report.differences) == {'수수료·세금 조회값', '보유·매도가능수량', '모의 계좌'}
    assert observed.account_tag not in repr(report)
