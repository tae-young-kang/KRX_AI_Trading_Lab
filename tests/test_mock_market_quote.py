from datetime import timedelta
import pytest

from app.kiwoom_mock_contract import ContractError
from app.mock_market_quote import parse_quote
from app.mock_market_budget import preview_buy
from test_mock_market_budget import NOW, inputs


def fixture():
    return {'return_code': 0, 'atn_stk_infr': [{'stk_cd': '900001',
        'dt': '20261019', 'cntr_tm': '100000', 'bid_tm': '100000',
        'cur_prc': '-1000', 'upl_pric': '+1300'}]}, {'cont-yn': 'N', 'next-key': ''}


def parse(payload, headers, **changes):
    args = dict(code='900001', started_at=NOW, fetched_at=NOW)
    args.update(changes)
    return parse_quote(payload, headers, **args)


def test_response_to_budget_preserves_trade_time_and_cannot_execute():
    payload, headers = fixture()
    quote, upper = parse(payload, headers, fetched_at=NOW+timedelta(seconds=2))
    assert quote.quote_at == NOW and quote.fetched_at != quote.quote_at
    capture, _, _, args = inputs()
    args['now'] = NOW+timedelta(seconds=2)
    result = preview_buy(capture, quote, upper, quantity=10, **args)
    assert result['reserved_won'] == 13010 and not result['executable']


@pytest.mark.parametrize('field,value', [('dt', None), ('dt','20261018'),
    ('dt','20260230'), ('cntr_tm','095954'), ('cntr_tm','100001'),
    ('cntr_tm',None), ('cntr_tm','250000'), ('stk_cd','900001_NX'),
    ('stk_cd','900002'), ('cur_prc','0'), ('cur_prc','1400'),
    ('cur_prc',True), ('upl_pric','1,300'), ('upl_pric','1000000000000')])
def test_bad_row_is_rejected_even_with_fresh_bid_time(field, value):
    payload, headers = fixture()
    payload['atn_stk_infr'][0][field] = value
    with pytest.raises(ContractError): parse(payload, headers)


@pytest.mark.parametrize('change', ['empty','duplicate','missing','continuation','wrong_api',
    'cursor','metadata_missing','slow','naive','backward','refused'])
def test_incomplete_response_and_clock_are_rejected(change):
    payload, headers = fixture()
    args = {}
    if change == 'empty': payload['atn_stk_infr'] = []
    elif change == 'duplicate': payload['atn_stk_infr'] *= 2
    elif change == 'missing': del payload['atn_stk_infr']
    elif change == 'continuation': headers['cont-yn'] = 'Y'
    elif change == 'wrong_api': headers['api-id'] = 'ka10001'
    elif change == 'cursor': headers['next-key'] = 'synthetic'
    elif change == 'metadata_missing': headers = {}
    elif change == 'slow': args['started_at'] = NOW-timedelta(seconds=6)
    elif change == 'naive': args['started_at'] = NOW.replace(tzinfo=None)
    elif change == 'backward': args['started_at'] = NOW+timedelta(seconds=1)
    elif change == 'refused': payload['return_code'] = 1
    with pytest.raises(ContractError): parse(payload, headers, **args)
