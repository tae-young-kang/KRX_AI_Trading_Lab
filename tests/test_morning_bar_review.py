"""Offline post-hoc daily-bar observation; never create a virtual fill."""
from datetime import datetime
from copy import deepcopy

import pytest

from app.backtest import Bar
from app.krx_history import HistoryStore
from app.morning_bar_review import observe, report_saved
from app.morning_request import setup_text
from app.morning_virtual import record_path
from app.storage import Database
from app.trading_clock import KST
from test_morning_venue_policy import new
from test_telegram_orders import sandbox, update

COHORT='2026-10-20'
REVIEW='2026-10-21'
NOW=datetime(2026,10,22,6,0,tzinfo=KST)


def daily(day,code='900001',market='KOSPI',opening=1000,high=1010,low=995,
          close=1005,volume=1000):
    return Bar(day,code,'합성',market,opening,high,low,close,volume,1,2000)


def installed(tmp_path):
    tracker,_=new(tmp_path)
    history=HistoryStore(tmp_path/'history.db')
    first=[
        daily(COHORT,'900001',opening=1000),
        daily(COHORT,'900002',market='KOSDAQ',opening=1000),
        daily(COHORT,'900003',opening=1000),
    ]
    review=[
        daily(REVIEW,'900001',high=1025,low=960,close=1010),
        daily(REVIEW,'900002',market='KOSDAQ',high=1010,low=985,close=998),
    ]
    history.insert_bars(first+review,'DEMO_SYNTHETIC')
    return tracker,history


def test_both_thresholds_hit_is_not_resolved_as_winning_or_losing_trade(tmp_path):
    tracker,history=installed(tmp_path)
    original=record_path(tmp_path,'DEMO',COHORT).read_bytes()
    report=report_saved(tmp_path,'DEMO',COHORT,REVIEW,
                        history=history,source='DEMO_SYNTHETIC',now=NOW)
    assert '익절·손절 가격대 모두 포함 · 선후 순서 미확인' in report
    assert '평가일의 일봉이 없어요' in report
    assert '평가일 고가·저가상 기준가격 미도달' in report
    assert '종가 변화 +1.000%' in report
    assert '실현손익을 기록하지 않아요' in report
    assert '가격 참고 가능 2/3종목 · 자료 누락/무효 1종목 · 익절·손절 동시 범위 1종목' in report
    assert tracker.db.trades()==[]
    assert record_path(tmp_path,'DEMO',COHORT).read_bytes()==original


def test_missing_entry_open_cannot_be_replaced_by_later_day_close(tmp_path):
    tracker,history=installed(tmp_path)
    with history.connect() as db:
        db.execute("DELETE FROM daily_bars WHERE source='DEMO_SYNTHETIC' AND day=? AND code=?",
                   (COHORT,'900001'))
    report=report_saved(tmp_path,'DEMO',COHORT,REVIEW,
                        history=history,source='DEMO_SYNTHETIC',now=NOW)
    assert '고정일의 일봉 시가가 없어요' in report
    assert '합성' not in report.splitlines()[0]
    assert tracker.db.trades()==[]


def test_zero_volume_never_claims_a_trade(tmp_path):
    tracker,history=installed(tmp_path)
    with history.connect() as db:
        db.execute("UPDATE daily_bars SET volume=0 WHERE source='DEMO_SYNTHETIC' AND day=? AND code=?",
                   (COHORT,'900001'))
    report=report_saved(tmp_path,'DEMO',COHORT,REVIEW,
                        history=history,source='DEMO_SYNTHETIC',now=NOW)
    assert '거래량이나 가격 근거가 부족해요' in report
    assert tracker.db.trades()==[]


def test_wrong_mode_missing_cohort_and_future_day_fail_without_writing(tmp_path):
    tracker,history=installed(tmp_path)
    assert '고정한 아침 추천 목록이 없어요' in report_saved(
        tmp_path,'MOCK',COHORT,REVIEW,history=history,source='KRX',now=NOW)
    assert not record_path(tmp_path,'MOCK',COHORT).exists()
    with pytest.raises(ValueError):
        report_saved(tmp_path,'DEMO',COHORT,REVIEW,
                     history=history,source='KRX',now=NOW)
    with pytest.raises(ValueError):
        report_saved(tmp_path,'DEMO',COHORT,'2026-10-22',
                     history=history,source='DEMO_SYNTHETIC',now=NOW)
    with pytest.raises(ValueError):
        report_saved(tmp_path,'DEMO',COHORT,'2026-10-19',
                     history=history,source='DEMO_SYNTHETIC',now=NOW)
    assert tracker.db.trades()==[]


def test_krx_unconfirmed_raw_snapshot_not_used_as_proven_open(tmp_path):
    from app.config import DEFAULTS
    from app.morning_virtual import MorningVirtual
    from app.morning_venue_policy import KRX_OPEN

    history=HistoryStore(tmp_path/'history.db')
    config=deepcopy(DEFAULTS)
    config.update(data_mode='KIWOOM',kiwoom_env='REAL',exit_take_pct=2,exit_stop_pct=3)
    tracker=MorningVirtual(Database(record_path(tmp_path,'REAL_QUOTES',COHORT)))
    tracker.prepare(day=COHORT,mode='REAL_QUOTES',origin='scan_1',
                    targets=[dict(code='900001',name='가상',market='KOSPI',nxt_eligible=None)],
                    settings=config,total_won=100000,now=datetime(2026,10,20,8,30,tzinfo=KST),
                    venue_policy=KRX_OPEN)
    history.insert_bars([daily(COHORT),daily(REVIEW)],'KRX')
    # No post-close fetched_days evidence was provided, so KRX source cannot
    # be promoted to a confirmed daily bar merely because OHLC rows exist.
    result=report_saved(tmp_path,'REAL_QUOTES',COHORT,REVIEW,
                        history=history,source='KRX',now=NOW)
    assert '고정일의 일봉 시가가 없어요' in result
    assert tracker.db.trades()==[]


def test_plan_is_never_changed_by_data_source_or_page_read(tmp_path):
    tracker,history=installed(tmp_path)
    root=record_path(tmp_path,'DEMO',COHORT)
    frozen=deepcopy(tracker.plan());original=root.read_bytes()
    report=report_saved(tmp_path,'DEMO',COHORT,REVIEW,
                        history=history,source='DEMO_SYNTHETIC',now=NOW,page=1)
    assert '1/1페이지' in report
    assert '1페이지까지' in report_saved(
        tmp_path,'DEMO',COHORT,REVIEW,
        history=history,source='DEMO_SYNTHETIC',now=NOW,page=2)
    assert tracker.plan()==frozen and root.read_bytes()==original


def test_legacy_nxt_cohort_is_rejected_not_backfilled_by_krx_daily_open(tmp_path):
    from test_morning_virtual import setup as legacy
    tracker,_=legacy(tmp_path/'legacy')
    plan=tracker.plan()
    with pytest.raises(ValueError,match='기존 NXT/KRX'):
        observe(plan,[],review_day=REVIEW,source='DEMO_SYNTHETIC')
    assert tracker.db.trades()==[]


def test_duplicate_or_malformed_ohlc_bars_are_rejected(tmp_path):
    tracker,_=installed(tmp_path)
    plan=tracker.plan()
    raw=[daily(COHORT),daily(REVIEW)]
    with pytest.raises(ValueError,match='중복'):
        observe(plan,raw+raw[:1],review_day=REVIEW,source='DEMO_SYNTHETIC')
    malformed=daily(REVIEW,high=998,opening=1000)
    with pytest.raises(ValueError,match='가격'):
        observe(plan,[daily(COHORT),malformed],review_day=REVIEW,source='DEMO_SYNTHETIC')
    assert tracker.db.trades()==[]


def test_telegram_command_is_read_only_with_owner_controls(tmp_path,sandbox):
    tracker,history=installed(tmp_path)
    database=Database(tmp_path/'practice.db')
    service,broker,_,clock=sandbox
    clock[0]=NOW
    service.morning_setup=lambda text:setup_text(
        tmp_path,database,history,'DEMO',{},text,now=clock[0])
    cmd=f'/가상일봉 {COHORT} {REVIEW} DEMO_SYNTHETIC'
    bad=update(sandbox,1,cmd)
    bad['message']['from']['id']=999
    assert service.handle(bad) is None
    message=service.handle(update(sandbox,2,cmd))
    assert '선후 순서 미확인' in message
    service.handle(update(sandbox,3,'/중지'))
    assert '선후 순서 미확인' in service.handle(update(sandbox,4,cmd))
    assert '사용법' in service.handle(update(sandbox,5,'/가상일봉 기타'))
    assert broker.calls==[] and tracker.db.trades()==[]


def test_price_evidence_summary_never_counts_extreme_invalid_return_as_observed(tmp_path):
    tracker,history=installed(tmp_path)
    with history.connect() as db:
        db.execute("UPDATE daily_bars SET open=1e-300,low=1e-300,high=1000,close=1000 "
                   "WHERE source='DEMO_SYNTHETIC' AND day=? AND code=?",
                   (COHORT,'900001'))
    text=report_saved(tmp_path,'DEMO',COHORT,REVIEW,
                      history=history,source='DEMO_SYNTHETIC',now=NOW)
    assert '가격 참고 가능 1/3종목 · 자료 누락/무효 2종목' in text
    assert '거래량이나 가격 근거가 부족해요' in text
    assert 'inf%' not in text and 'nan%' not in text
    assert tracker.db.trades()==[]
