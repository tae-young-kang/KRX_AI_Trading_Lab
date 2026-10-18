"""Offline transport tests: no broker login, quotes, orders or messages."""
import sqlite3
import time
from unittest.mock import Mock

import pytest
import requests

from app.config import Settings
from app.krx_history import HistoryStore
from app.market import DemoMarket, KiwoomMarket, recommendation_text
from app.storage import Database
from app.strategy import evaluate
from app.quote_batch_profile import QuoteBatchProfile


def quote(code):
    return dict(stk_cd=code, cur_prc='10,000', flu_rt='3.0',
                trde_prica='20000', pred_trde_qty_pre='250',
                mac='5000', trde_qty='2000000', dt='20261016', cntr_tm='153000')


@pytest.fixture
def broker(monkeypatch):
    # These are local dummy inputs; the session is replaced before any call.
    client = KiwoomMarket('dummy', 'dummy')
    client.token = 'dummy'
    client.token_ts = time.time()
    monkeypatch.setattr('app.market.time.sleep', lambda seconds: None)
    client.session.post = Mock(side_effect=AssertionError('unexpected network request'))
    return client


def response(data, more=False, key=''):
    result = Mock(status_code=200)
    result.json.return_value = {'return_code': 0, **data}
    result.headers = {'cont-yn': 'Y' if more else 'N', 'next-key': key}
    return result


@pytest.mark.parametrize('selection,expected', [
    ('전체', {'KOSPI', 'KOSDAQ'}), ('KOSPI', {'KOSPI'}), ('KOSDAQ', {'KOSDAQ'})])
def test_all_symbols_across_pages_and_market_selection(broker, tmp_path, selection, expected):
    listings = {'0': [f'{i:06d}' for i in range(1, 46)],
                '10': [f'{i:06d}' for i in range(101, 146)]}
    requested_markets = []
    requested_codes = []

    def post(url, headers, json, timeout):
        assert url.endswith('/api/dostk/stkinfo')
        if headers['api-id'] == 'ka10099':
            market = json['mrkt_tp']
            if market == '8':
                return response({'list': [dict(code='999999', name='FAKE ETF')]})
            requested_markets.append(market)
            codes = listings[market]
            if headers['cont-yn'] == 'N':
                return response({'list': [dict(code=c, name='FAKE') for c in codes[:20]]}, True, 'page2')
            assert headers['next-key'] == 'page2'
            return response({'list': [dict(code=c, name='FAKE') for c in codes[20:]]})
        assert headers['api-id'] == 'ka10095'
        assert len(json['stk_cd'].split('|')) <= 100
        codes = json['stk_cd'].split('|')
        if headers['cont-yn'] == 'N':
            requested_codes.extend(codes)
            return response({'atn_stk_infr': [quote(c) for c in codes[:len(codes)//2]]}, True, 'quote2')
        assert headers['next-key'] == 'quote2'
        return response({'atn_stk_infr': [quote(c) for c in codes[len(codes)//2:]]})

    broker.session.post.side_effect = post
    progress = []
    stocks = broker.scan(selection, progress=progress.append)
    assert len(stocks) == (90 if selection == '전체' else 45)
    assert {s.market for s in stocks} == expected
    assert len(set(requested_codes)) == len(stocks)
    assert set(requested_markets) == ({'0', '10'} if selection == '전체' else
                                      {'0'} if selection == 'KOSPI' else {'10'})
    assert progress[-1].endswith('누락 0개')
    assert all(s.trade_value_eok == 200 and s.cap_eok == 5000 and s.vol_ratio_pct == 250 for s in stocks)
    assert all(s.quote_stamp == '2026-10-16T15:30:00' for s in stocks)
    assert '2026-10-16T15:30:00' in recommendation_text(stocks[:1])
    results = evaluate(stocks, Settings(tmp_path).values)
    assert len(results) == len(stocks) and all(r.passed for r in results)


def test_missing_quote_blocks_recommendations(broker):
    broker.session.post.side_effect = [
        response({'list': [dict(code='900001'), dict(code='900002')]}),
        response({'list': [dict(code='999999')]}),
        response({'atn_stk_infr': [quote('900001')]})]
    with pytest.raises(RuntimeError, match='누락'):
        broker.scan('KOSPI')


def test_successful_batch_hint_reduces_rejected_calls_without_caching_quotes(broker):
    broker.batch_profile=QuoteBatchProfile()
    codes=[f'{i:06d}' for i in range(1,201)]
    calls=[]
    round_number=[0]
    omit_last=[False]
    def post(url,headers,json,timeout):
        if headers['api-id']=='ka10099':
            rows=[dict(code=code,name='시험') for code in codes] if json['mrkt_tp']=='0' else [dict(code='999999',name='시험ETF')]
            return response(dict(list=rows))
        batch=json['stk_cd'].split('|')
        calls.append(len(batch))
        if len(batch)>25: return response(dict(return_code=1))
        return response(dict(atn_stk_infr=[dict(quote(code),cur_prc=str(10000+round_number[0])) for code in batch
                                         if not omit_last[0] or code!=codes[-1]]))
    broker.session.post.side_effect=post
    first=broker.scan('KOSPI')
    first_count=len(calls);calls.clear();round_number[0]=1
    second=broker.scan('KOSPI')
    assert first_count==11 and len(calls)==8 and max(calls)==25
    assert {row.code for row in first}=={row.code for row in second}==set(codes)
    assert all(row.price==10001 for row in second)
    broker.batch_profile=QuoteBatchProfile()
    omit_last[0]=True
    with pytest.raises(RuntimeError,match='누락'):
        broker.scan('KOSPI')
    assert broker.batch_profile.limit(broker.url)==100


def test_batch_hint_expiry_clock_reversal_and_server_scope():
    clock=[0]
    profile=QuoteBatchProfile(clock=lambda:clock[0])
    profile.completed('mock',25)
    assert profile.limit('mock')==25 and profile.limit('real')==100
    clock[0]=600
    assert profile.limit('mock')==100
    clock[0]=-1
    assert profile.limit('mock')==100


@pytest.mark.parametrize('key', ['', 'same'])
def test_broken_or_repeated_continuation_is_rejected(broker, key):
    broker.session.post.return_value = response({'list': [dict(code='900001')]}, True, key)
    broker.session.post.side_effect = None
    with pytest.raises(RuntimeError, match='연속조회'):
        broker.scan('KOSPI')


def test_empty_selected_market_is_not_silently_skipped(broker):
    broker.session.post.side_effect = [response({'list': [dict(code='900001')]}), response({'list': []})]
    with pytest.raises(RuntimeError, match='비어'):
        broker.scan('전체')


def test_cancellation_does_not_return_partial_results(broker):
    broker.session.post.side_effect = [response({'list': [dict(code='900001')]}),
                                       response({'list': [dict(code='999999')]}),
                                       response({'atn_stk_infr': [quote('900001')]})]
    cancelled = False

    def progress(message):
        nonlocal cancelled
        if '시세 조회 1개' in message:
            cancelled = True

    with pytest.raises(RuntimeError, match='취소'):
        broker.scan('KOSPI', progress=progress, cancelled=lambda: cancelled)


def test_read_only_network_retry_and_error_sanitization(broker):
    broker.session.post.side_effect = [requests.Timeout('sensitive request details'),
                                       response({'list': []})]
    assert broker.query('ka10099', '/api/dostk/stkinfo', {})['list'] == []
    assert broker.session.post.call_count == 2
    broker.session.post.side_effect = requests.ConnectionError('sensitive request details')
    with pytest.raises(RuntimeError, match='3회') as ex:
        broker.query('ka10099', '/api/dostk/stkinfo', {})
    assert 'sensitive' not in str(ex.value)


def test_nonzero_api_result_is_not_success(broker):
    broker.session.post.side_effect = None
    broker.session.post.return_value = response({'return_code': 20, 'return_msg': 'sensitive'})
    with pytest.raises(RuntimeError, match='조회 실패') as ex:
        broker.scan('KOSPI')
    assert 'sensitive' not in str(ex.value)


def test_legacy_candidate_limit_is_ignored(tmp_path):
    (tmp_path / 'settings.json').write_text('{"candidate_limit": 30, "market": "KOSDAQ"}')
    cfg = Settings(tmp_path).values
    assert cfg['market'] == 'KOSDAQ' and 'candidate_limit' not in cfg
    assert len(DemoMarket().scan()) == len(DemoMarket.BASE)


@pytest.mark.parametrize('store_class', [Database, HistoryStore])
def test_transaction_closes_connection_and_rolls_back(tmp_path, store_class):
    store = store_class(tmp_path / 'offline.db')
    with store.connect() as conn:
        conn.execute('CREATE TABLE lifecycle (value INTEGER)')
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute('SELECT 1')
    with pytest.raises(ValueError):
        with store.connect() as conn:
            conn.execute('INSERT INTO lifecycle VALUES (1)')
            raise ValueError('rollback')
    with store.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM lifecycle').fetchone()[0] == 0


def test_4307_symbols_need_only_44_quote_requests(broker):
    codes = [f'{i:06d}' for i in range(1, 4308)]
    batches = []

    def post(url, headers, json, timeout):
        if headers['api-id'] == 'ka10099':
            listing = ['999999'] if json['mrkt_tp'] == '8' else codes
            return response({'list': [dict(code=c, name='FAKE') for c in listing]})
        batch = json['stk_cd'].split('|')
        batches.append(batch)
        return response({'atn_stk_infr': [quote(c) for c in batch]})

    broker.session.post.side_effect = post
    stocks = broker.scan('KOSPI')
    assert len(stocks) == 4307
    assert len(batches) == 44
    assert [c for batch in batches for c in batch] == codes


@pytest.mark.parametrize('include_etf', [True, False])
def test_official_etf_membership_controls_quote_universe(broker, tmp_path, include_etf):
    # Names intentionally misleading: classification must use list type 8.
    broker.session.post.side_effect = [
        response({'list': [dict(code='900001', name='ordinary-looking ETF'),
                            dict(code='900002', name='ETF word but ordinary stock')]}),
        response({'list': [dict(code='900001', name='official ETF')]}),
        response({'atn_stk_infr': [quote(c) for c in
                                  (['900001', '900002'] if include_etf else ['900002'])]})]
    stocks = broker.scan('KOSPI', include_etf=include_etf)
    assert {s.code for s in stocks} == ({'900001', '900002'} if include_etf else {'900002'})
    if include_etf:
        etf = next(s for s in stocks if s.code == '900001')
        assert etf.is_etf and ' · ETF' in recommendation_text([etf])
        cfg = Settings(tmp_path).values
        cfg['include_etf'] = False
        assert not evaluate([etf], cfg)[0].passed


def test_empty_etf_classification_fails_closed(broker):
    broker.session.post.side_effect = [response({'list': [dict(code='900001')]}),
                                       response({'list': []})]
    with pytest.raises(RuntimeError, match='ETF 분류'):
        broker.scan('KOSPI', include_etf=False)


@pytest.mark.parametrize('failure', ['rejected', 'truncated'])
def test_large_batch_recovery_never_drops_symbols(broker, failure):
    codes = [f'{i:06d}' for i in range(1, 101)]
    calls = []

    def post(url, headers, json, timeout):
        batch = json['stk_cd'].split('|')
        calls.append(batch)
        if len(batch) > 10:
            if failure == 'rejected':
                return response({'return_code': 7})
            batch = batch[:10]
        return response({'atn_stk_infr': [quote(c) for c in batch]})

    broker.session.post.side_effect = post
    rows = broker._quote_rows(codes, None, lambda text: None)
    assert len(rows) == 100 and {r['stk_cd'] for r in rows} == set(codes)
    assert len(calls) > 1


def test_unrecoverable_missing_symbol_still_blocks_large_batch(broker):
    codes = [f'{i:06d}' for i in range(1, 101)]

    def post(url, headers, json, timeout):
        return response({'atn_stk_infr': [quote(c) for c in json['stk_cd'].split('|') if c != codes[-1]]})

    broker.session.post.side_effect = post
    with pytest.raises(RuntimeError, match='누락'):
        broker._quote_rows(codes, None, lambda text: None)


@pytest.mark.parametrize('flag,expected',[('Y',True),('N',None),('',None),(None,None),(True,None),(1,None)])
def test_nxt_positive_observation_is_captured_without_guessing_negative(broker,flag,expected):
    from datetime import datetime
    def post(url,headers,json,timeout):
        if headers['api-id']=='ka10099':
            if json['mrkt_tp']=='8': return response({'list':[dict(code='999999',name='FAKE ETF')]})
            return response({'list':[dict(code='900001',name='연습',nxtEnable=flag)]})
        return response({'atn_stk_infr':[quote('900001')]})
    broker.session.post.side_effect=post
    stock=broker.scan('KOSPI')[0]
    assert stock.nxt_eligible is expected and stock.nxt_source=='KIWOOM_KA10099'
    assert datetime.fromisoformat(stock.nxt_observed_at).utcoffset().total_seconds()==32400
    assert stock.as_dict()['nxt_eligible'] is expected
