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
            requested_markets.append(market)
            codes = listings[market]
            if headers['cont-yn'] == 'N':
                return response({'list': [dict(code=c, name='FAKE') for c in codes[:20]]}, True, 'page2')
            assert headers['next-key'] == 'page2'
            return response({'list': [dict(code=c, name='FAKE') for c in codes[20:]]})
        assert headers['api-id'] == 'ka10095'
        assert len(json['stk_cd']) <= 20
        codes = json['stk_cd'].split('|')
        if headers['cont-yn'] == 'N':
            requested_codes.extend(codes)
            return response({'atn_stk_infr': [quote(codes[0])]}, len(codes) > 1, 'quote2')
        assert headers['next-key'] == 'quote2'
        return response({'atn_stk_infr': [quote(codes[1])]})

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
        response({'atn_stk_infr': [quote('900001')]})]
    with pytest.raises(RuntimeError, match='누락'):
        broker.scan('KOSPI')


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
