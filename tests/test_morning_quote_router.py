"""Supplied synthetic trades, temporary records only; no clients or orders."""
from copy import deepcopy
from dataclasses import asdict,replace
from datetime import datetime,timedelta
import json
from pathlib import Path
import pytest
from app.config import DEFAULTS
from app.morning_quote_router import route_trade
from app.morning_virtual import MorningVirtual,VenueTrade,record_path
from app.morning_virtual_cli import replay
from app.storage import Database
from app.trading_clock import KST


def make(root,day='2026-10-19',mode='DEMO',*,enter=True,venue='NXT',code='900001'):
    cfg=deepcopy(DEFAULTS)
    cfg.update(data_mode='DEMO' if mode=='DEMO' else 'KIWOOM',kiwoom_env='REAL' if mode=='REAL_QUOTES' else 'MOCK',
               exit_take_pct=2,exit_stop_pct=3,paper_fee_pct=0,paper_sell_tax_pct=0,paper_slippage_pct=0)
    frozen=datetime.fromisoformat(day+'T07:30:00+09:00')
    args=dict(day=day,mode=mode,origin='scan_test',
        targets=[dict(code=code,name='합성 예제',market='KOSPI',nxt_eligible=venue=='NXT')],
        settings=cfg,total_won=10000,now=frozen)
    tracker=MorningVirtual(Database(record_path(root,mode,day)));tracker.prepare(**args)
    at=frozen.replace(hour=8 if venue=='NXT' else 9,minute=0)
    evidence=VenueTrade(mode,code,venue,1000,at,at,'FIRST_TRADE','a'*64)
    if enter:tracker.enter(evidence,now=at)
    return tracker,args,evidence


def mark(day='2026-10-20',price=1010):
    at=datetime.fromisoformat(day+'T08:01:00+09:00')
    return VenueTrade('DEMO','900001','NXT',price,at,at,'TRADE','b'*64)


def bytes_of(*trackers):return [tracker.db.path.read_bytes() for tracker in trackers]


def test_router_marks_today_and_carryover_then_exits_each_independent_holding(tmp_path):
    old,_,_=make(tmp_path);today,_,_=make(tmp_path,'2026-10-20')
    waiting,_,_=make(tmp_path,'2026-10-16',enter=False)
    other,_,_=make(tmp_path,'2026-10-20','MOCK')
    before=bytes_of(waiting,other)
    tick=mark()
    rows=route_trade(tmp_path,tick,now=tick.at)
    assert {r['day']:r['state'] for r in rows}=={
        '2026-10-16':'WAITING_FIRST_TRADE','2026-10-19':'HOLD','2026-10-20':'HOLD'}
    assert '미실현손익 추정 +100.00원' in old.daily_text('2026-10-20')
    assert '미실현손익 추정 +100.00원' in today.daily_text('2026-10-20')
    assert bytes_of(waiting,other)==before
    close=replace(tick,price_won=1020,at=tick.at+timedelta(seconds=1),fetched_at=tick.at+timedelta(seconds=1))
    rows=route_trade(tmp_path,close,now=close.at)
    assert [r['pnl'] for r in rows if r['state']=='VIRTUAL_CLOSED']==[200,200]
    assert len(old.db.trades())==len(today.db.trades())==1
    again=route_trade(tmp_path,close,now=close.at)
    assert [r['state'] for r in again].count('ALREADY_CLOSED')==2


@pytest.mark.parametrize('bad',[
    {'kind':'FIRST_TRADE'},{'kind':'OTHER'},{'mode':'LIVE'},{'code':'900001_NX'},
    {'venue':'SOR'},{'price_won':True},{'price_won':0},{'evidence_hash':'bad'},
    {'at':datetime(2026,10,20,8,1)}, {'fetched_at':datetime(2026,10,20,8,1)},
])
def test_bad_common_input_cannot_change_any_record(tmp_path,bad):
    first,_,_=make(tmp_path);second,_,_=make(tmp_path,'2026-10-20')
    tick=mark();before=bytes_of(first,second)
    with pytest.raises(ValueError):route_trade(tmp_path,replace(tick,**bad),now=tick.at)
    assert bytes_of(first,second)==before


@pytest.mark.parametrize('case',['stale','future','prior_day','out_of_session','closed_day'])
def test_time_and_trading_day_validation_precedes_all_writes(tmp_path,case):
    tracker,_,_=make(tmp_path);before=bytes_of(tracker);tick=mark();now=tick.at
    if case=='stale':now+=timedelta(seconds=31)
    elif case=='future':tick=replace(tick,fetched_at=tick.at+timedelta(seconds=1))
    elif case=='prior_day':now+=timedelta(days=1)
    elif case=='out_of_session':
        at=tick.at.replace(hour=7);tick=replace(tick,at=at,fetched_at=at);now=at
    else:
        at=tick.at.replace(day=18);tick=replace(tick,at=at,fetched_at=at);now=at
    with pytest.raises(ValueError):route_trade(tmp_path,tick,now=now)
    assert bytes_of(tracker)==before


def test_missing_unheld_wrong_venue_and_wrong_target_never_create_or_write(tmp_path):
    tick=mark();empty=tmp_path/'empty'
    assert route_trade(empty,tick,now=tick.at)==[] and not empty.exists()
    tracker,_,_=make(tmp_path,enter=False);before=bytes_of(tracker)
    assert route_trade(tmp_path,tick,now=tick.at)[0]['state']=='WAITING_FIRST_TRADE'
    wrong=replace(tick,venue='KRX',at=tick.at.replace(hour=9),fetched_at=tick.at.replace(hour=9))
    assert route_trade(tmp_path,wrong,now=wrong.at)[0]['state']=='SKIPPED_VENUE'
    assert route_trade(tmp_path,replace(tick,code='900002'),now=tick.at)[0]['state']=='SKIPPED_NOT_TARGET'
    assert bytes_of(tracker)==before and tracker.db.trades()==[]


def test_broken_cohort_is_explicit_failure_without_blocking_other_holdings(tmp_path):
    broken,_,_=make(tmp_path);good,_,_=make(tmp_path,'2026-10-20')
    with broken.db.connect() as db:
        db.execute("UPDATE morning_cohort SET payload='{}'")
    before=bytes_of(broken);tick=mark(price=1020)
    rows=route_trade(tmp_path,tick,now=tick.at)
    assert rows[0]['state']=='FAILED' and rows[1]['state']=='VIRTUAL_CLOSED'
    assert str(tmp_path) not in json.dumps(rows) and bytes_of(broken)==before
    assert '실현손익 +200.00원' in good.daily_text('2026-10-20')


def test_wrong_mode_payload_and_future_cohort_are_never_written(tmp_path):
    bad,_,_=make(tmp_path);future,_,_=make(tmp_path,'2026-10-21')
    with bad.db.connect() as db:
        plan=MorningVirtual(bad.db).plan();plan['mode']='MOCK'
        db.execute('UPDATE morning_cohort SET payload=?',(json.dumps(plan),))
    before=bytes_of(bad,future);tick=mark()
    assert [r['state'] for r in route_trade(tmp_path,tick,now=tick.at)]==['FAILED']
    assert bytes_of(bad,future)==before


@pytest.mark.parametrize('field,value',[('source','KIWOOM'),('opened_at','2026-10-18T08:00:00+09:00'),('entry_json','{}')])
def test_holding_identity_mismatch_is_not_updated(tmp_path,field,value):
    tracker,_,_=make(tmp_path)
    with tracker.db.connect() as db:db.execute('UPDATE paper_trades SET '+field+'=?',(value,))
    before=bytes_of(tracker);tick=mark()
    assert route_trade(tmp_path,tick,now=tick.at)[0]['state']=='FAILED'
    assert bytes_of(tracker)==before


def event(trade):
    row=asdict(trade);row.update(at=trade.at.isoformat(),fetched_at=trade.fetched_at.isoformat())
    return row


def test_cli_replay_general_trade_updates_carryover_without_new_entry(tmp_path):
    old,_,_=make(tmp_path)
    _,args,opening=make(tmp_path/'fixtures','2026-10-20',enter=False)
    plan={key:value for key,value in args.items() if key!='now'};plan['frozen_at']=args['now'].isoformat()
    tick=mark(price=1020)
    assert replay(tmp_path,plan,[event(opening),event(tick)],now=tick.at)==['VIRTUAL_ENTERED','VIRTUAL_CLOSED']
    assert '실현손익 +200.00원' in old.daily_text('2026-10-20')
    assert replay(tmp_path,plan,[event(opening),event(tick)],now=tick.at)==['VIRTUAL_ENTERED','ALREADY_CLOSED']
    assert len(old.db.trades())==1


def test_cli_reports_other_cohort_failure_instead_of_claiming_success(tmp_path):
    broken,_,_=make(tmp_path)
    with broken.db.connect() as db:db.execute("UPDATE morning_cohort SET payload='{}'")
    _,args,opening=make(tmp_path/'fixtures','2026-10-20',enter=False)
    plan={key:value for key,value in args.items() if key!='now'};plan['frozen_at']=args['now'].isoformat()
    tick=mark(price=1020)
    with pytest.raises(ValueError,match='일부 아침 목록'):
        replay(tmp_path,plan,[event(opening),event(tick)],now=tick.at)
    today=MorningVirtual(Database(record_path(tmp_path,'DEMO','2026-10-20'),read_only=True))
    assert today.db.trades()[0]['closed_at'] is not None


def test_cli_cross_mode_evidence_is_rejected_before_any_database_creation_or_change(tmp_path):
    mock,_,_=make(tmp_path,'2026-10-20','MOCK')
    _,args,_=make(tmp_path/'fixtures','2026-10-20',enter=False)
    plan={key:value for key,value in args.items() if key!='now'};plan['frozen_at']=args['now'].isoformat()
    tick=replace(mark(price=1020),mode='MOCK');before=bytes_of(mock)
    with pytest.raises(ValueError,match='시세 모드가 달라요'):
        replay(tmp_path,plan,[event(tick)],now=tick.at)
    assert bytes_of(mock)==before and mock.db.trades()[0]['closed_at'] is None
    assert not record_path(tmp_path,'DEMO','2026-10-20').exists()


@pytest.mark.parametrize('mismatch',['venue','symbol'])
def test_cli_selected_target_mismatch_cannot_update_matching_carryover(tmp_path,mismatch):
    old,_,_=make(tmp_path)
    selected,args,_=make(tmp_path,'2026-10-20',enter=False,
        venue='KRX' if mismatch=='venue' else 'NXT',code='900002' if mismatch=='symbol' else '900001')
    plan={key:value for key,value in args.items() if key!='now'};plan['frozen_at']=args['now'].isoformat()
    tick=mark(price=1020);before=bytes_of(old,selected)
    with pytest.raises(ValueError,match='거래 시장이 달라요'):
        replay(tmp_path,plan,[event(tick)],now=tick.at)
    assert bytes_of(old,selected)==before and old.db.trades()[0]['closed_at'] is None


def test_cli_target_mismatch_is_detected_in_all_events_before_creating_selected_database(tmp_path):
    old,_,_=make(tmp_path)
    _,args,opening=make(tmp_path/'fixtures','2026-10-20',enter=False)
    plan={key:value for key,value in args.items() if key!='now'};plan['frozen_at']=args['now'].isoformat()
    tick=replace(mark(price=1020),code='900002');before=bytes_of(old)
    with pytest.raises(ValueError,match='종목·거래 시장이 달라요'):
        replay(tmp_path,plan,[event(opening),event(tick)],now=tick.at)
    assert bytes_of(old)==before and not record_path(tmp_path,'DEMO','2026-10-20').exists()


def test_parent_link_is_rejected_without_touching_link_target(tmp_path,monkeypatch):
    tracker,_,_=make(tmp_path);before=bytes_of(tracker);tick=mark()
    original=Path.is_symlink
    monkeypatch.setattr(Path,'is_symlink',lambda p:p==tmp_path/'morning_virtual' or original(p))
    with pytest.raises(ValueError,match='링크'):route_trade(tmp_path,tick,now=tick.at)
    assert bytes_of(tracker)==before


def test_windows_junction_file_is_cohort_failure_without_writing(tmp_path,monkeypatch):
    tracker,_,_=make(tmp_path);before=bytes_of(tracker);tick=mark()
    monkeypatch.setattr(Path,'is_junction',lambda p:p==tracker.db.path,raising=False)
    assert route_trade(tmp_path,tick,now=tick.at)[0]['state']=='FAILED'
    assert bytes_of(tracker)==before
