"""Offline-only evidence inventory for frozen v2 cohorts. No broker calls."""
from dataclasses import replace
from datetime import datetime

import pytest

from app.morning_open_evidence_status import inspect, render, report_saved
from app.morning_request import setup_text
from app.morning_virtual import record_path
from app.storage import Database
from app.trading_clock import KST
from test_morning_venue_policy import new
from test_morning_virtual import trade, setup
from test_telegram_orders import sandbox, update


DAY='2026-10-20'
NOW=datetime(2026,10,22,6,0,tzinfo=KST)


def test_frozen_cohort_without_first_evidence_never_becomes_a_fill(tmp_path):
    tracker,_=new(tmp_path)
    path=record_path(tmp_path,'DEMO',DAY)
    before=path.read_bytes()
    quality=inspect(tmp_path,'DEMO',DAY,now=NOW)
    assert (quality['stored'],quality['missing'],quality['virtual_open'],quality['virtual_closed'])==(0,3,0,0)
    assert all(x['state']=='NO_STORED_FIRST_EVIDENCE' for x in quality['results'])
    text=render(quality)
    assert '소프트웨어 근거 첨부 0종목 · 미첨부 3종목' in text
    assert '거래소 공식 첫 체결 인증' in text
    assert '첫 거래 근거 미첨부 · 진입 미확인' in text
    assert path.read_bytes()==before and tracker.db.trades()==[]


def test_software_attached_entry_is_not_called_verified_exchange_price(tmp_path):
    tracker,_=new(tmp_path)
    opened=datetime(2026,10,20,9,0,tzinfo=KST)
    evidence=replace(trade(venue='KRX',hour=9),at=opened,fetched_at=opened)
    result=tracker.enter(evidence,now=opened)
    assert result['state']=='VIRTUAL_ENTERED'
    plan=inspect(tmp_path,'DEMO',DAY,now=NOW)
    assert (plan['stored'],plan['missing'],plan['virtual_open'])==(1,2,1)
    text=render(plan)
    assert '가상 진입 근거 기록 있음 · 가상 보유' in text
    assert '거래소 공식 첫 체결 인증이나 가격의 진위 검증은 아니에요' in text
    assert '실제 계좌 체결 아님' in text
    assert '소프트웨어 근거 첨부 1종목 · 미첨부 2종목' in text


def test_unreadable_identity_fails_closed_without_new_file(tmp_path):
    tracker,_=new(tmp_path)
    root=record_path(tmp_path,'DEMO',DAY)
    opened=datetime(2026,10,20,9,0,tzinfo=KST)
    original=replace(trade(venue='KRX',hour=9),at=opened,fetched_at=opened)
    tracker.enter(original,now=opened)
    with tracker.db.connect() as db:
        db.execute("UPDATE paper_trades SET entry_json='{}' WHERE code='900001'")
    with pytest.raises(ValueError,match='근거'):
        inspect(tmp_path,'DEMO',DAY,now=NOW)
    assert root.is_file()
    assert not record_path(tmp_path,'MOCK',DAY).exists()
    assert inspect(tmp_path,'MOCK',DAY,now=NOW) is None


def test_original_legacy_cohort_not_misreported_as_krx_open(tmp_path):
    _,args=setup(tmp_path/'old_data')
    from app.morning_virtual import MorningVirtual
    old=MorningVirtual(Database(record_path(tmp_path,'DEMO',args['day'])))
    old.prepare(**args)
    report=inspect(tmp_path,'DEMO',args['day'],now=NOW)
    assert report['legacy'] is True
    assert '이전 NXT/KRX 혼합 정책' in render(report)
    # No evidence is manufactured for a cohort which has not been created.
    assert not record_path(tmp_path,'MOCK',DAY).is_file()


def test_future_day_invalid_date_and_bad_page_are_rejected(tmp_path):
    new(tmp_path)
    with pytest.raises(ValueError):
        inspect(tmp_path,'DEMO','2030-10-20',now=NOW)
    with pytest.raises(ValueError):
        inspect(tmp_path,'DEMO','2026-02-30',now=NOW)
    with pytest.raises(ValueError):
        render(inspect(tmp_path,'DEMO',DAY,now=NOW),page=0)
    assert '1페이지까지' in report_saved(tmp_path,'DEMO',DAY,now=NOW,page=2)


def test_telegram_owner_can_read_while_order_entry_disabled(tmp_path,sandbox):
    tracker,_=new(tmp_path)
    database=Database(tmp_path/'practice.db')
    service,broker,_,clock=sandbox
    clock[0]=NOW
    service.morning_setup=lambda text:setup_text(
        tmp_path,database,None,'DEMO',{},text,now=clock[0])
    cmd=f'/가상시초근거 {DAY}'
    outsider=update(sandbox,1,cmd)
    outsider['message']['from']['id']=999
    assert service.handle(outsider) is None
    assert '미첨부 3종목' in service.handle(update(sandbox,2,cmd))
    service.handle(update(sandbox,3,'/중지'))
    assert '미첨부 3종목' in service.handle(update(sandbox,4,cmd))
    assert '사용법' in service.handle(update(sandbox,5,'/가상시초근거 기타'))
    assert not tracker.db.trades() and broker.calls==[]


def test_expired_missing_day_returns_no_database_or_synthetic_evidence(tmp_path):
    root=tmp_path/'does-not-exist'
    result=report_saved(root,'DEMO',DAY,now=NOW)
    assert '고정된 추천 목록이 없어요' in result
    assert not root.exists()
