from datetime import timedelta
import pytest
from app.krx_history import HistoryStore
from app.theme_kiwoom import collect_theme
from app.theme_pruning_cache import load
from app.theme_sources import KIWOOM_SOURCES
from test_theme_kiwoom import Client,NOW
from test_kiwoom_daily import DAYS


class MixedClient(Client):
    def query(self,*args):
        data,headers=super().query(*args)
        if data['stk_cd']=='900002':
            for row in data['stk_dt_pole_chart_qry']:row['high_pric']='10400'
        return data,headers


def test_repeated_search_reuses_history_without_alternating_full_scan(tmp_path):
    history=HistoryStore(tmp_path/'history.db')
    first=MixedClient()
    original=collect_theme(history,first,'REAL_QUOTES',now=NOW)
    assert first.calls==['900001','900002'] and original['matched']==1
    for _ in range(3):
        client=MixedClient();messages=[]
        result=collect_theme(history,client,'REAL_QUOTES',now=NOW,progress=messages.append)
        assert client.calls==['900001'] and result['matched']==1
        assert result['search_scope']['excluded_count']==1
        assert [r['code'] for r in result['results']]==[r['code'] for r in original['results']]
        assert any('건너뜀' in message for message in messages)
    with history.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM theme_pruning_bars WHERE code='900002'").fetchone()[0]==7
        assert db.execute("SELECT COUNT(*) FROM daily_bars WHERE source=? AND code='900002'",
            (KIWOOM_SOURCES['REAL_QUOTES'],)).fetchone()[0]==0


def test_real_history_cannot_prune_mock_server(tmp_path):
    history=HistoryStore(tmp_path/'history.db')
    collect_theme(history,MixedClient(),'REAL_QUOTES',now=NOW)
    client=MixedClient();client.url='https://mockapi.kiwoom.com'
    collect_theme(history,client,'MOCK',now=NOW)
    assert client.calls==['900001','900002']


def test_incomplete_history_is_queried_not_eliminated(tmp_path):
    history=HistoryStore(tmp_path/'history.db')
    collect_theme(history,MixedClient(),'REAL_QUOTES',now=NOW)
    with history.connect() as db:
        for table in ('daily_bars','theme_pruning_bars'):
            db.execute(f'DELETE FROM {table} WHERE code=? AND day=?',('900002',DAYS[2]))
    client=MixedClient()
    collect_theme(history,client,'REAL_QUOTES',now=NOW)
    assert client.calls==['900001','900002']


def test_failure_or_cancel_does_not_publish_partial_pruning_cache(tmp_path):
    history=HistoryStore(tmp_path/'history.db')
    collect_theme(history,MixedClient(),'REAL_QUOTES',now=NOW)
    with history.connect() as db:before=[tuple(r) for r in db.execute('SELECT * FROM theme_pruning_bars ORDER BY source,day,market,code')]
    client=MixedClient()
    with pytest.raises(ValueError):
        collect_theme(history,client,'REAL_QUOTES',now=NOW+timedelta(minutes=1),cancelled=lambda:bool(client.calls))
    with history.connect() as db:after=[tuple(r) for r in db.execute('SELECT * FROM theme_pruning_bars ORDER BY source,day,market,code')]
    assert after==before


@pytest.mark.parametrize('stamp',[
    '2026-10-19T13:00:00+09:00','2026-10-21T21:00:00+09:00','2026-10-19T21:00:00'])
def test_unfinished_future_or_timezone_unknown_snapshot_not_used(tmp_path,stamp):
    history=HistoryStore(tmp_path/'history.db')
    collect_theme(history,MixedClient(),'REAL_QUOTES',now=NOW)
    with history.connect() as db:
        for table in ('daily_bars','theme_pruning_bars'):
            db.execute(f'UPDATE {table} SET fetched_at=? WHERE day=?',(stamp,DAYS[-1]))
    rows=load(history,KIWOOM_SOURCES['REAL_QUOTES'],DAYS,NOW)
    assert not any(b.date==DAYS[-1] for b in rows)


def test_closed_day_snapshot_can_seed_next_trading_day(tmp_path):
    history=HistoryStore(tmp_path/'history.db')
    collect_theme(history,MixedClient(),'REAL_QUOTES',now=NOW)
    rows=load(history,KIWOOM_SOURCES['REAL_QUOTES'],DAYS[1:],NOW+timedelta(days=1))
    assert len(rows)==12 and sum(b.date==DAYS[-1] for b in rows)==2
