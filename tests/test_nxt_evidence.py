from datetime import datetime
from dataclasses import replace
import pytest
from app.market import Stock
from app.backtest import Bar
from app.krx_history import HistoryStore
from app.nxt_evidence import record
from app.theme_store import prepare,latest_report
from app.recommendation_outcomes import SessionEvidence
from app.trading_clock import KST


def fixture(tmp_path):
    history=HistoryStore(tmp_path/'hist.db')
    days=('2026-10-08','2026-10-12','2026-10-13','2026-10-14','2026-10-15','2026-10-16','2026-10-19')
    history.insert_bars([Bar(d,'900001','연습','KOSPI',100,110,99,100,1000,100,4000) for d in days],'CSV_USER')
    args=dict(sessions=SessionEvidence(days,days[0],days[-1],'USER_CONFIRMED'),through=days[-1],mode='MOCK',source='CSV_USER')
    stock=Stock('900001','연습','KOSPI',100,3,200,150,4000,source='KIWOOM',nxt_eligible=True,
                nxt_observed_at='2026-10-19T10:00:00+09:00',nxt_source='KIWOOM_KA10099')
    return history,args,stock


def test_same_day_mode_observation_is_frozen_in_theme_and_unknown_overrides(tmp_path):
    history,args,stock=fixture(tmp_path);now=datetime(2026,10,20,12,tzinfo=KST)
    record(history,'MOCK',[stock],now=now)
    prepare(HistoryStore(history.path),**args)
    old=latest_report(history,mode='MOCK')
    assert 'NXT 거래 가능 ·' in old and 'NXT 관측 시각' in old
    record(history,'MOCK',[replace(stock,nxt_eligible=None,nxt_observed_at='2026-10-19T11:00:00+09:00')],now=now)
    assert latest_report(history,mode='MOCK')==old
    assert prepare(history,**args)['prepared']==1
    assert 'NXT 거래 가능 여부 미확인' in latest_report(history,mode='MOCK')


def test_other_day_and_other_mode_cannot_prove_detection_date(tmp_path):
    history,args,stock=fixture(tmp_path);now=datetime(2026,10,20,12,tzinfo=KST)
    record(history,'REAL_QUOTES',[stock],now=now)
    record(history,'MOCK',[replace(stock,nxt_observed_at='2026-10-20T10:00:00+09:00')],now=now)
    prepare(history,**args)
    assert 'NXT 거래 가능 여부 미확인' in latest_report(history,mode='MOCK')
    with pytest.raises(ValueError):record(history,'DEMO',[stock],now=now)


def test_conflicting_same_instant_is_not_overwritten(tmp_path):
    history,_,stock=fixture(tmp_path);now=datetime(2026,10,20,12,tzinfo=KST)
    record(history,'MOCK',[stock],now=now)
    with pytest.raises(ValueError,match='같은 시각'):
        record(history,'MOCK',[replace(stock,nxt_eligible=None)],now=now)
    with history.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM nxt_observations').fetchone()[0]==1
        assert db.execute('SELECT eligible FROM nxt_observations').fetchone()[0]==1


@pytest.mark.parametrize('stamp',['broken','2026-10-18T10:00:00+09:00','2026-10-19T10:00:00'])
def test_invalid_persisted_nxt_context_cannot_create_new_theme_result(tmp_path,stamp):
    history,args,stock=fixture(tmp_path);now=datetime(2026,10,20,12,tzinfo=KST)
    record(history,'MOCK',[stock],now=now)
    with history.connect() as db:
        db.execute('UPDATE nxt_observations SET observed_at=?',(stamp,))
    with pytest.raises(ValueError,match='저장된 NXT'):prepare(history,**args)
    with history.connect() as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='theme_runs'").fetchone()


def test_nxt_latest_selection_uses_instants_not_text_offsets():
    from app.nxt_evidence import latest_for_day
    positive=dict(mode='MOCK',day='2026-10-19',market='KOSPI',code='900001',observed_at='2026-10-19T10:00:00+09:00',eligible=1)
    unknown={**positive,'observed_at':'2026-10-19T02:00:00+00:00','eligible':None}
    result=latest_for_day([unknown,positive],'MOCK','2026-10-19')
    assert result[('KOSPI','900001')]['eligible'] is None
