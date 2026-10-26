from datetime import datetime,timedelta
from dataclasses import replace
from hashlib import sha256
import pytest
from app.morning_day import report
from app.morning_virtual import MorningVirtual,record_path
from app.storage import Database
from app.trading_clock import KST
from test_morning_virtual import setup,trade,DAY,PRE
from test_morning_request import rig,call,NOW
from test_telegram_orders import sandbox,update


def make(root,day=DAY):
    _,args=setup(root/'fixtures'/day)
    args.update(day=day,now=datetime.fromisoformat(day+'T07:30:00+09:00'))
    tracker=MorningVirtual(Database(record_path(root,'DEMO',day)))
    tracker.prepare(**args)
    return tracker


def test_day_view_includes_prior_cohort_exit_and_current_cohort_separately(tmp_path):
    old=make(tmp_path);new=make(tmp_path,'2026-10-20')
    opening=trade();old.enter(opening,now=opening.at)
    at=opening.at+timedelta(days=1,minutes=1)
    closing=replace(opening,price_won=1020,at=at,fetched_at=at,kind='TRADE')
    old.mark(closing,now=at)
    before={p:sha256(p.read_bytes()).hexdigest() for p in (record_path(tmp_path,'DEMO',DAY),record_path(tmp_path,'DEMO','2026-10-20'))}
    first=report(tmp_path,'DEMO','2026-10-20',now=at)
    second=report(tmp_path,'DEMO','2026-10-20',now=at,page=2)
    assert '아침 목록 2개' in first and '현재 목록 2026-10-19' in first
    assert '실현손익 +200.00원' in first and '승 1 / 패 0' in first
    assert '다음: /가상하루 2026-10-20 2' in first
    assert '현재 목록 2026-10-20' in second and '0% 수익이 아닙니다' in second
    assert '이전: /가상하루 2026-10-20 1' in second
    assert '/연습결산' not in first
    assert {p:sha256(p.read_bytes()).hexdigest() for p in before}==before
    historical=report(tmp_path,'DEMO',DAY,now=at)
    assert '실현손익 +0.00원' in historical and '아침 목록 1개' in historical


def test_carryover_without_daily_quote_remains_unknown_and_other_modes_absent(tmp_path):
    tracker=make(tmp_path);opening=trade();tracker.enter(opening,now=opening.at)
    now=PRE+timedelta(days=1,hours=13)
    text=report(tmp_path,'DEMO','2026-10-20',now=now)
    assert '해당일 보유 1종목' in text and '미실현손익 추정 미확인' in text
    assert '당일 가격 없음 1종목' in text
    assert '목록이 없어요' in report(tmp_path,'MOCK','2026-10-20',now=now)
    assert not (tmp_path/'morning_virtual'/'MOCK').exists()


def test_dates_pages_and_wrong_mode_payload_are_not_silently_accepted(tmp_path):
    make(tmp_path)
    with pytest.raises(ValueError):report(tmp_path,'DEMO','2026-10-21',now=PRE)
    with pytest.raises(ValueError):report(tmp_path,'DEMO',DAY,now=PRE,page=True)
    assert '1페이지까지' in report(tmp_path,'DEMO',DAY,now=PRE,page=2)
    tracker=MorningVirtual(Database(record_path(tmp_path,'DEMO',DAY)))
    with tracker.db.connect() as db:
        db.execute("UPDATE morning_cohort SET payload=replace(payload,'DEMO','MOCK')")
    with pytest.raises(ValueError):report(tmp_path,'DEMO',DAY,now=PRE)


def test_telegram_day_view_is_private_read_only_and_available_when_stopped(tmp_path,sandbox):
    db,history,cfg=rig(tmp_path);make(tmp_path)
    service,broker,_,clock=sandbox
    clock[0]=NOW
    service.morning_setup=lambda text:call(tmp_path,db,history,cfg,text,clock[0])
    bad=update(sandbox,1,'/가상하루');bad['message']['from']['id']=99
    assert service.handle(bad) is None
    service.handle(update(sandbox,2,'/중지'))
    assert '결산일 2026-10-20' in service.handle(update(sandbox,3,'/가상하루'))
    assert '사용법' in service.handle(update(sandbox,4,'/가상하루 2026-10-20 잘못된페이지'))
    assert broker.calls==[]


def test_symbol_pages_use_day_view_navigation_without_losing_sixth_symbol(tmp_path):
    _,args=setup(tmp_path/'fixtures')
    args['targets']=[dict(code=f'90000{i}',name=f'예제{i}',market='KOSPI',nxt_eligible=True) for i in range(1,7)]
    tracker=MorningVirtual(Database(record_path(tmp_path,'DEMO',DAY)))
    tracker.prepare(**args)
    for i in range(1,7):
        evidence=trade(code=f'90000{i}')
        tracker.enter(evidence,now=evidence.at)
    now=PRE.replace(hour=21)
    first=report(tmp_path,'DEMO',DAY,now=now)
    second=report(tmp_path,'DEMO',DAY,now=now,page=2)
    assert '1/2페이지' in first and '예제6 (900006)' not in first
    assert '예제6 (900006)' in second and '종목 내역 2/2' in second
    assert '/연습결산' not in first+second
