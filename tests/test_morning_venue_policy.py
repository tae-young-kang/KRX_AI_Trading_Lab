"""New KRX policy and immutable legacy cohorts, temporary synthetic data only."""
from copy import deepcopy
from dataclasses import asdict,replace
from datetime import datetime,timedelta
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from app.config import DEFAULTS
from app.morning_virtual import MorningVirtual,record_path
from app.morning_venue_policy import KRX_OPEN,deadline,krx_open,stored_policy
from app.morning_quote_router import _plan_identity,route_trade
from app.morning_virtual_cli import replay
from app.morning_request import next_day,latest_targets,setup_text,freeze_scheduled
from app.morning_schedule import MorningSchedule
from app.storage import Database
from app.trading_clock import KST
from test_morning_virtual import setup,trade
from test_morning_request import rig,NOW


def new(root,*,frozen=None):
    _,args=setup(root/'fixtures')
    frozen=frozen or datetime(2026,10,20,8,30,tzinfo=KST)
    args.update(day='2026-10-20',now=frozen)
    args['targets']+=[dict(code='900003',name='미확인 예제',market='KOSPI',nxt_eligible=None)]
    tracker=MorningVirtual(Database(record_path(root,'DEMO','2026-10-20')))
    tracker.prepare(**args,venue_policy=KRX_OPEN)
    return tracker,args


def event(evidence):
    result=asdict(evidence);result.update(at=evidence.at.isoformat(),fetched_at=evidence.fetched_at.isoformat());return result


def special_open(monkeypatch):
    import exchange_calendars
    original=exchange_calendars.get_calendar
    def shifted(*args,**kwargs):
        calendar=original(*args,**kwargs)
        return SimpleNamespace(sessions=calendar.sessions,
            session_open=lambda day:calendar.session_open(day)+timedelta(hours=1))
    krx_open.cache_clear();monkeypatch.setattr(exchange_calendars,'get_calendar',shifted)


def test_new_v2_krx_policy_retains_nxt_metadata_and_never_overwrites_legacy(tmp_path):
    tracker,args=new(tmp_path)
    plan=tracker.plan()
    assert plan['version']==2 and stored_policy(plan)==KRX_OPEN
    assert [r['venue'] for r in plan['targets']]==['KRX']*3
    assert [r['nxt_eligible'] for r in plan['targets']]==[True,False,None]
    assert tracker.prepare(**args,venue_policy=KRX_OPEN)==plan
    with pytest.raises(ValueError):tracker.prepare(**args,venue_policy='NXT_FIRST')
    old,old_args=setup(tmp_path/'old');before=old.db.path.read_bytes()
    with pytest.raises(ValueError):old.prepare(**old_args,venue_policy=KRX_OPEN)
    assert old.db.path.read_bytes()==before and old.plan()['version']==1
    old_args['now']=old_args['now'].replace(hour=8)
    with pytest.raises(ValueError):old.prepare(**old_args)


def test_actual_opening_cutoff_and_unknown_calendar_never_guess(tmp_path,monkeypatch):
    special_open(monkeypatch)
    try:
        assert krx_open('2026-10-20').hour==10
        before=datetime(2026,10,20,9,30,tzinfo=KST)
        assert next_day(before)=='2026-10-20'
        assert next_day(before.replace(hour=10,minute=0))=='2026-10-21'
        tracker,args=new(tmp_path,frozen=before)
        tick=replace(trade(hour=9,minute=50),at=before.replace(hour=9,minute=50),fetched_at=before.replace(hour=9,minute=50),venue='KRX')
        with pytest.raises(ValueError,match='개장 이후'):tracker.enter(tick,now=tick.at)
        tick=replace(tick,at=before.replace(hour=10,minute=0),fetched_at=before.replace(hour=10,minute=0))
        assert tracker.enter(tick,now=tick.at)['state']=='VIRTUAL_ENTERED'
        pre=replace(tick,kind='TRADE',at=before.replace(hour=9,minute=59),fetched_at=before.replace(hour=9,minute=59))
        with pytest.raises(ValueError,match='개장 이후'):tracker.mark(pre,now=pre.at)
        args['now']=before.replace(hour=10,minute=0)
        with pytest.raises(ValueError):tracker.prepare(**args,venue_policy=KRX_OPEN)
    finally:krx_open.cache_clear()
    import exchange_calendars
    monkeypatch.setattr(exchange_calendars,'get_calendar',lambda *a,**k:(_ for _ in ()).throw(ValueError()))
    with pytest.raises(ValueError,match='추측하지'):deadline('2026-10-21',KRX_OPEN)


@pytest.mark.parametrize('change',['version','unknown_policy','non_krx','missing_metadata'])
def test_v2_identity_rejects_unsupported_or_inconsistent_policy(tmp_path,change):
    tracker,_=new(tmp_path);plan=tracker.plan()
    if change=='version':plan['version']=1
    elif change=='unknown_policy':plan['venue_policy']='AUTO'
    elif change=='non_krx':plan['targets'][0]['venue']='NXT'
    else:plan['targets'][0].pop('nxt_eligible')
    with pytest.raises(ValueError):_plan_identity(plan,'DEMO','2026-10-20')
    destination=tmp_path/'replay'
    with pytest.raises(ValueError):replay(destination,plan,[],now=datetime(2026,10,20,12,tzinfo=KST))
    assert not destination.exists()


def test_new_v2_replay_krx_and_legacy_nxt_carryover_keep_own_markets(tmp_path):
    _,old_args=setup(tmp_path/'oldfixtures')
    old=MorningVirtual(Database(record_path(tmp_path,'DEMO','2026-10-19')));old.prepare(**old_args)
    first=trade();old.enter(first,now=first.at)
    today,_=new(tmp_path)
    plan=today.plan()
    opening=replace(trade(venue='KRX',hour=9),at=datetime(2026,10,20,9,tzinfo=KST),fetched_at=datetime(2026,10,20,9,tzinfo=KST))
    closing=replace(opening,price_won=1020,kind='TRADE',at=opening.at+timedelta(minutes=1),fetched_at=opening.at+timedelta(minutes=1))
    assert replay(tmp_path,plan,[event(opening),event(closing)],now=closing.at)==['VIRTUAL_ENTERED','VIRTUAL_CLOSED']
    assert old.db.trades()[0]['closed_at'] is None
    nxt=replace(closing,venue='NXT');rows=route_trade(tmp_path,nxt,now=nxt.at)
    assert [r['state'] for r in rows]==['VIRTUAL_CLOSED','SKIPPED_VENUE']
    assert old.plan()['targets'][0]['venue']=='NXT' and today.plan()['venue_policy']==KRX_OPEN


def test_original_v1_replay_input_metadata_remains_ignored_and_database_unchanged(tmp_path):
    _,args=setup(tmp_path/'fixtures')
    tracker=MorningVirtual(Database(record_path(tmp_path,'DEMO',args['day'])))
    # This is the original replay's stored shape: selection_info was ignored.
    tracker.prepare(**args)
    plan={key:value for key,value in args.items() if key!='now'}
    plan['frozen_at']=args['now'].isoformat()
    plan['selection_info']=dict(kind='GENERAL',asof=args['now'].isoformat())
    before=tracker.db.path.read_bytes()
    assert replay(tmp_path,plan,[],now=args['now'])==[]
    assert tracker.db.path.read_bytes()==before and 'selection_info' not in tracker.plan()


def test_v2_replay_preserves_selection_metadata(tmp_path):
    tracker,_=new(tmp_path/'fixtures');plan=tracker.plan()
    plan['selection_info']=dict(kind='GENERAL',asof=plan['frozen_at'])
    destination=tmp_path/'replay'
    assert replay(destination,plan,[],now=datetime(2026,10,20,8,30,tzinfo=KST))==[]
    saved=MorningVirtual(Database(record_path(destination,'DEMO',plan['day']),read_only=True))
    assert saved.plan()['selection_info']==plan['selection_info']


def test_public_existing_legacy_cohort_keeps_version_and_bytes(tmp_path):
    db,history,cfg=rig(tmp_path)
    origin,targets,settings,info=latest_targets(db,history,'DEMO','일반',cfg,NOW)
    tracker=MorningVirtual(Database(record_path(tmp_path,'DEMO','2026-10-20')))
    tracker.prepare(day='2026-10-20',mode='DEMO',origin=origin,targets=targets,settings=settings,
        total_won=10000,now=NOW,selection_info=info)
    before=tracker.db.path.read_bytes()
    text=setup_text(tmp_path,db,history,'DEMO',cfg,'/가상추적 일반 1만원',now=NOW.replace(hour=8,minute=30))
    assert '이전 정책' in text and tracker.db.path.read_bytes()==before and tracker.plan()['version']==1
    result=freeze_scheduled(tmp_path,db,history,'DEMO',cfg,'2026-10-20','/가상추적 일반 1만원',now=NOW.replace(hour=8,minute=30))
    assert result['venue_policy'] is None and tracker.db.path.read_bytes()==before


def test_legacy_cohort_schedule_completion_uses_legacy_deadline(tmp_path):
    db,history,cfg=rig(tmp_path)
    origin,targets,settings,info=latest_targets(db,history,'DEMO','일반',cfg,NOW)
    tracker=MorningVirtual(Database(record_path(tmp_path,'DEMO','2026-10-20')))
    tracker.prepare(day='2026-10-20',mode='DEMO',origin=origin,targets=targets,settings=settings,
        total_won=10000,now=NOW,selection_info=info)
    before=tracker.db.path.read_bytes();at=NOW.replace(hour=8,minute=30)
    schedule=MorningSchedule(tmp_path/'jobs.db','owner','DEMO')
    schedule.handle('/아침가상예약 일반 1만원',now=at)
    result=schedule.tick(now=at,clock=lambda:at,active=lambda:True,
        prepare=lambda day,text:freeze_scheduled(tmp_path,db,history,'DEMO',cfg,day,text,now=at),send=lambda text:1)
    assert result=='LATE' and tracker.db.path.read_bytes()==before


def test_theme_plan_uses_latest_krx_policy_without_rewriting_detection_or_budget(tmp_path):
    from app.theme_budget import plan_text,preview_latest,preview_text
    from test_theme_delivery import setup as theme_setup
    history,_=theme_setup(tmp_path)
    before=Path(history.path).read_bytes()
    plan=preview_latest(history,mode='DEMO',total_won=10000,fee_bps=2,buffer_bps=200)
    assert plan['venue_policy']==KRX_OPEN
    text=plan_text(history,'DEMO','/테마1계획 전체 1만원')
    assert 'NXT 가능 여부와 관계없이 전 종목 KRX 시초가 시장가' in text
    assert '전 종목 KRX 시초가 시장가 기준' in preview_text(plan)
    assert '주문 안 함' in text and Path(history.path).read_bytes()==before


def test_scheduled_freeze_allows_830_then_dynamic_special_open_completion(tmp_path,monkeypatch):
    db,history,cfg=rig(tmp_path);special_open(monkeypatch)
    at=datetime(2026,10,20,9,30,tzinfo=KST)
    try:
        schedule=MorningSchedule(tmp_path/'jobs.db','owner','DEMO')
        schedule.handle('/아침가상예약 일반 1만원',now=at)
        result=schedule.tick(now=at,clock=lambda:at,active=lambda:True,
            prepare=lambda day,text:freeze_scheduled(tmp_path,db,history,'DEMO',cfg,day,text,now=at),send=lambda text:1)
        assert result=='FROZEN'
        tracker=MorningVirtual(Database(record_path(tmp_path,'DEMO','2026-10-20'),read_only=True))
        assert tracker.plan()['venue_policy']==KRX_OPEN and tracker.plan()['frozen_at'].startswith('2026-10-20T09:30')
        # The persisted attempt remains final even after the deadline.
        assert schedule.tick(now=at.replace(hour=10,minute=0),clock=lambda:at,active=lambda:True,
            prepare=lambda *a:pytest.fail('repeat'),send=lambda text:pytest.fail('resend'))=='FROZEN'
    finally:krx_open.cache_clear()
