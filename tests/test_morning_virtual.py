from copy import deepcopy
from dataclasses import replace
from datetime import datetime,timedelta
import json
import sqlite3
import pytest
from app.config import DEFAULTS
from app.storage import Database
from app.morning_virtual import MorningVirtual,VenueTrade
from app.trading_clock import KST
from test_telegram_orders import sandbox,update

DAY='2026-10-19'
PRE=datetime(2026,10,19,7,30,tzinfo=KST)


def setup(tmp_path):
    tracker=MorningVirtual(Database(tmp_path/'virtual.db'))
    cfg=deepcopy(DEFAULTS)
    cfg.update(exit_take_pct=2,exit_stop_pct=3,paper_fee_pct=0,paper_sell_tax_pct=0,paper_slippage_pct=0)
    targets=[dict(code='900001',name='넥스트 예제',market='KOSPI',nxt_eligible=True),
             dict(code='900002',name='정규장 예제',market='KOSDAQ',nxt_eligible=False)]
    args=dict(day=DAY,mode='DEMO',origin='scan-example',targets=targets,settings=cfg,total_won=20000,now=PRE)
    tracker.prepare(**args)
    return tracker,args


def trade(code='900001',venue='NXT',price=1000,hour=8,minute=0,kind='FIRST_TRADE'):
    at=datetime(2026,10,19,hour,minute,tzinfo=KST)
    return VenueTrade('DEMO',code,venue,price,at,at,kind,'a'*64)


def test_frozen_cohort_nxt_krx_entry_exits_and_daily_profit(tmp_path):
    tracker,args=setup(tmp_path)
    nxt=trade();krx=trade('900002','KRX',hour=9)
    assert tracker.enter(nxt,now=nxt.at)['quantity']==10
    assert tracker.enter(krx,now=krx.at)['quantity']==10
    assert tracker.enter(nxt,now=nxt.at)['quantity']==10
    assert len(tracker.db.trades())==2
    args['settings'].update(exit_take_pct=99,exit_stop_pct=99)
    take=trade(price=1020,minute=1,kind='TRADE')
    stop=trade('900002','KRX',970,hour=9,minute=1,kind='TRADE')
    assert tracker.mark(take,now=take.at)['pnl']==200
    assert tracker.mark(stop,now=stop.at)['pnl']==-300
    assert tracker.mark(stop,now=stop.at)['state']=='ALREADY_CLOSED'
    report=tracker.daily_text(DAY)
    assert '실현손익 -100.00원' in report and '승 1 / 패 1' in report
    assert '익절 1 / 손절 1' in report and '미실현손익 추정 +0.00원' in report
    for record in tracker.db.trades():
        data=json.loads(record['entry_json'])
        assert data['venue'] in ('NXT','KRX') and data['price_basis']=='VENUE_FIRST_TRADE'
        assert data['evidence_hash']=='a'*64 and record['opened_at'].startswith(DAY)


@pytest.mark.parametrize('change',['wrong_venue','not_first','wrong_mode','stale','no_hash'])
def test_no_entry_without_matching_first_trade_evidence(tmp_path,change):
    tracker,_=setup(tmp_path);evidence=trade();now=evidence.at
    if change=='wrong_venue':evidence=replace(evidence,venue='KRX',at=evidence.at.replace(hour=9),fetched_at=evidence.at.replace(hour=9));now=evidence.at
    elif change=='not_first':evidence=replace(evidence,kind='TRADE')
    elif change=='wrong_mode':evidence=replace(evidence,mode='REAL_QUOTES')
    elif change=='stale':now+=timedelta(seconds=31)
    else:evidence=replace(evidence,evidence_hash='')
    with pytest.raises(ValueError):tracker.enter(evidence,now=now)
    assert tracker.db.trades()==[]


def test_no_backfilled_selection_no_overwrite_and_entry_conflicts(tmp_path):
    tracker,args=setup(tmp_path)
    assert tracker.prepare(**args)==tracker.plan()
    with pytest.raises(ValueError):tracker.prepare(**dict(args,now=PRE.replace(hour=9)))
    with pytest.raises(ValueError):tracker.prepare(**dict(args,total_won=10000))
    evidence=trade();tracker.enter(evidence,now=evidence.at)
    with pytest.raises(ValueError):tracker.enter(replace(evidence,price_won=999),now=evidence.at)
    assert len(tracker.db.trades())==1


def test_daily_hold_unrealized_and_missing_next_day_quote_are_distinct(tmp_path):
    tracker,_=setup(tmp_path);evidence=trade()
    tracker.enter(evidence,now=evidence.at)
    mark=trade(price=1010,minute=1,kind='TRADE')
    assert tracker.mark(mark,now=mark.at)['state']=='HOLD'
    assert '미실현손익 추정 +100.00원' in tracker.daily_text(DAY)
    assert '미실현손익 추정 미확인' in tracker.daily_text('2026-10-20')
    with pytest.raises(ValueError):tracker.mark(trade(kind='TRADE'),now=evidence.at)


def test_unknown_eligibility_never_defaults_to_krx(tmp_path):
    tracker=MorningVirtual(Database(tmp_path/'unknown.db'))
    _,args=setup(tmp_path/'other')
    args['targets'][0]['nxt_eligible']=None
    tracker.prepare(**args)
    evidence=trade()
    with pytest.raises(ValueError):tracker.enter(evidence,now=evidence.at)
    assert tracker.db.trades()==[]


def test_supplied_evidence_replay_and_telegram_read_only_report(tmp_path,sandbox):
    from dataclasses import asdict
    from app.morning_virtual_cli import replay
    from app.morning_virtual import report_saved,record_path
    _,args=setup(tmp_path/'source')
    plan={k:v for k,v in args.items() if k!='now'}
    plan['frozen_at']=PRE.isoformat()
    evidence=[trade(),trade(price=1020,minute=1,kind='TRADE')]
    events=[]
    for item in evidence:
        event=asdict(item)
        event.update(at=item.at.isoformat(),fetched_at=item.fetched_at.isoformat())
        events.append(event)
    root=tmp_path/'records'
    now=PRE.replace(hour=12)
    assert replay(root,plan,events,now=now)==['VIRTUAL_ENTERED','VIRTUAL_CLOSED']
    assert replay(root,plan,events,now=now)==['VIRTUAL_ENTERED','ALREADY_CLOSED']
    path=record_path(root,'DEMO',DAY)
    before=path.read_bytes()
    service,broker,_,_=sandbox
    service.morning_daily=lambda day,page=1:report_saved(root,'DEMO',day,page=page)
    bad=update(sandbox,1,'/가상결산 '+DAY);bad['message']['from']['id']=99
    assert service.handle(bad) is None
    event=update(sandbox,2,'/가상결산 '+DAY)
    reply=service.handle(event)
    assert service.handle(event)==reply and '가상 추적' in reply
    assert '+200.00원' in reply and broker.calls==[]
    assert path.read_bytes()==before
    assert '기록이 없어요' in report_saved(root,'MOCK',DAY)


def test_cohort_does_not_accept_existing_manual_practice_positions(tmp_path):
    tracker,args=setup(tmp_path)
    evidence=trade();tracker.enter(evidence,now=evidence.at)
    with tracker.db.connect() as db:db.execute('DELETE FROM morning_cohort')
    with pytest.raises(ValueError,match='별도'):
        tracker.prepare(**args)


def test_read_only_database_never_initializes_or_allows_writes(tmp_path):
    missing=tmp_path/'missing'/'virtual.db'
    database=Database(missing,read_only=True)
    with pytest.raises(sqlite3.OperationalError):database.connect()
    assert not missing.parent.exists()
    tracker,_=setup(tmp_path/'existing')
    reader=Database(tracker.db.path,read_only=True)
    before=tracker.db.path.read_bytes()
    with pytest.raises(sqlite3.OperationalError):reader.log('TEST','must not write')
    assert tracker.db.path.read_bytes()==before


def test_full_cohort_over_500_preserves_equal_budget_and_last_symbol_result(tmp_path):
    tracker=MorningVirtual(Database(tmp_path/'large.db'))
    cfg=deepcopy(DEFAULTS)
    cfg.update(exit_take_pct=2,exit_stop_pct=3,paper_fee_pct=0,paper_sell_tax_pct=0,paper_slippage_pct=0)
    targets=[dict(code=str(900000+i),name=f'전체대상{i}',market='KOSPI',nxt_eligible=True) for i in range(501)]
    args=dict(day=DAY,mode='DEMO',origin='theme1_501',targets=targets,settings=cfg,total_won=501000,now=PRE)
    plan=tracker.prepare(**args)
    assert len(plan['targets'])==501 and plan['targets'][-1]['code']=='900500'
    assert tracker.prepare(**args)==plan
    for code in ('900000','900500'):
        evidence=trade(code=code)
        assert tracker.enter(evidence,now=evidence.at)['quantity']==1
    winner=trade(code='900500',price=1020,minute=1,kind='TRADE')
    loser=trade(code='900000',price=970,minute=1,kind='TRADE')
    assert tracker.mark(winner,now=winner.at)['pnl']==20
    assert tracker.mark(loser,now=loser.at)['pnl']==-30
    report=tracker.daily_text(DAY)
    assert '고정 추천 501종목' in report and '미진입 499종목' in report
    assert '실현손익 -10.00원' in report and '승 1 / 패 1' in report
    assert '전체대상500' in report


def test_full_saved_theme_selection_is_not_limited_to_500(tmp_path):
    from app.morning_request import setup_text
    from app.krx_history import HistoryStore
    from app.theme_store import prepare
    from app.recommendation_outcomes import SessionEvidence
    from app.backtest import Bar
    from app.morning_virtual import record_path
    from test_kiwoom_daily import DAYS
    root=tmp_path/'records';history=HistoryStore(tmp_path/'theme.db')
    bars=[Bar(day,str(900000+i),f'전체대상{i}','KOSPI',100,110,99,100,1000,100,4000)
          for i in range(501) for day in DAYS]
    history.insert_bars(bars,'DEMO')
    result=prepare(history,sessions=SessionEvidence(tuple(DAYS),DAYS[0],DAYS[-1],'DEMO'),
        through=DAYS[-1],mode='DEMO',source='DEMO')
    assert result['matched']==501
    now=PRE.replace(hour=21)
    text=setup_text(root,Database(tmp_path/'general.db'),history,'DEMO',deepcopy(DEFAULTS),
                    '/가상추적 테마1 501만원',now=now)
    assert '고정 종목 501개' in text
    tracker=MorningVirtual(Database(record_path(root,'DEMO','2026-10-20'),read_only=True))
    assert len(tracker.plan()['targets'])==501 and tracker.db.trades()==[]
    assert tracker.plan()['total_won']==5010000
