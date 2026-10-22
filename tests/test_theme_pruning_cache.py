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


def test_failure_or_cancel_does_not_replace_completed_pruning_cache(tmp_path):
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


class LowFirstClient(Client):
    def query(self,*args):
        data,headers=super().query(*args)
        if data['stk_cd']=='900001':
            for item in data['stk_dt_pole_chart_qry']:item['high_pric']='10400'
        return data,headers


@pytest.mark.parametrize('interruption',['cancel','failure'])
def test_interrupted_scan_keeps_only_past_hints_and_restart_prunes_without_partial_alert(tmp_path,interruption):
    from app.theme_delivery import latest_snapshot
    history=HistoryStore(tmp_path/'history.db')
    class Broken(LowFirstClient):
        def query(self,*args):
            if args[2]['stk_cd']=='900002' and interruption=='failure':raise RuntimeError('synthetic connection failure')
            return super().query(*args)
    client=Broken()
    with pytest.raises((ValueError,RuntimeError)):
        collect_theme(history,client,'REAL_QUOTES',now=NOW,
            cancelled=(lambda:bool(client.calls)) if interruption=='cancel' else None)
    source=KIWOOM_SOURCES['REAL_QUOTES']
    with history.connect() as db:
        hints=db.execute('SELECT day,code FROM theme_pruning_checkpoints').fetchall()
        assert len(hints)==6 and all(r['day']<DAYS[-1] and r['code']=='900001' for r in hints)
        assert db.execute('SELECT COUNT(*) FROM daily_bars').fetchone()[0]==0
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='theme_runs'").fetchone()
    # Reopen as a new process would, without carrying any buffered response.
    reopened=HistoryStore(history.path);next_client=LowFirstClient()
    result=collect_theme(reopened,next_client,'REAL_QUOTES',now=NOW)
    assert next_client.calls==['900002'] and result['matched']==1
    assert result['search_scope']['universe_count']==2 and result['search_scope']['excluded_count']==1
    assert [r['code'] for r in result['results']]==['900002']
    assert latest_snapshot(reopened,'REAL_QUOTES')['run_id']==result['run_id']
    assert reopened.count(source)[0]==7
    reference=collect_theme(HistoryStore(tmp_path/'fresh.db'),LowFirstClient(),'REAL_QUOTES',now=NOW)
    assert [r['code'] for r in reference['results']]==[r['code'] for r in result['results']]


def test_interrupted_real_hints_never_prune_mock_scan(tmp_path):
    history=HistoryStore(tmp_path/'history.db');client=LowFirstClient()
    with pytest.raises(ValueError):
        collect_theme(history,client,'REAL_QUOTES',now=NOW,cancelled=lambda:bool(client.calls))
    mock=LowFirstClient();mock.url='https://mockapi.kiwoom.com'
    result=collect_theme(history,mock,'MOCK',now=NOW)
    assert mock.calls==['900001','900002'] and result['matched']==1


def test_checkpoint_rejects_current_day_invalid_bar_and_wrong_source(tmp_path):
    from app.theme_pruning_cache import checkpoint
    from app.backtest import Bar
    from dataclasses import replace
    history=HistoryStore(tmp_path/'history.db')
    valid=Bar(DAYS[-2],'900001','합성','KOSPI',100,110,99,100,1000,100,None)
    source=KIWOOM_SOURCES['REAL_QUOTES']
    for bars,server,stamp in [([replace(valid,date=DAYS[-1])],source,NOW.isoformat()),
                              ([replace(valid,high=90)],source,NOW.isoformat()),
                              ([valid],'KRX',NOW.isoformat()),([valid],source,'2026-10-19T21:00:00')]:
        with pytest.raises(ValueError):checkpoint(history,server,bars,stamp)
    with history.connect() as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='theme_pruning_checkpoints'").fetchone()
