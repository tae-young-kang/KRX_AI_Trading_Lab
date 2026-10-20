"""Temporary user-input records vs virtual cohorts; never broker IO."""
from copy import deepcopy
from datetime import datetime
import sqlite3
import pytest
from app.config import DEFAULTS
from app.storage import Database
from app.morning_virtual import MorningVirtual,record_path,VenueTrade
from app.morning_reconciliation import report
from app.actual_trade_records import import_csv
from app.morning_request import setup_text
from app.trading_clock import KST
from test_actual_trade_records import row,blob
from test_telegram_orders import sandbox,update

DAY='2026-10-20';NOW=datetime(2026,10,20,12,tzinfo=KST)


def setup(tmp_path):
    cfg=dict(deepcopy(DEFAULTS),data_mode='KIWOOM',kiwoom_env='REAL')
    tracker=MorningVirtual(Database(record_path(tmp_path,'REAL_QUOTES',DAY)))
    tracker.prepare(day=DAY,mode='REAL_QUOTES',origin='scan_1',settings=cfg,total_won=70000,
        now=NOW.replace(hour=7,minute=30),targets=[dict(code=f'90000{i}',name=f'추천{i}',market='KOSPI',nxt_eligible=True) for i in range(1,8)])
    return tracker,cfg


def test_same_day_code_buy_inputs_and_virtual_entries_stay_separate_and_read_only(tmp_path):
    tracker,cfg=setup(tmp_path);actual=tmp_path/'actual_fill_records.db'
    import_csv(actual,blob(row(filled_at=DAY+'T09:00:00+09:00'),
        row('second',filled_at=DAY+'T09:01:00+09:00',quantity='5',price_won='1100',venue='KRX'),
        row('sell',filled_at=DAY+'T09:02:00+09:00',code='900002',side='SELL'),
        row('other-day',code='900003'),row('unrelated',filled_at=DAY+'T09:00:00+09:00',code='900099')),now=NOW)
    at=NOW.replace(hour=8)
    tracker.enter(VenueTrade('REAL_QUOTES','900001','NXT',1000,at,at,'FIRST_TRADE','a'*64),now=at)
    before=(actual.read_bytes(),tracker.db.path.read_bytes())
    text=report(tmp_path,'REAL_QUOTES',DAY,now=NOW)
    assert '실제 매수 입력 1종목' in text and '매수 입력 15주 · 15,500원' in text and 'KRX/NXT' in text
    assert '별도 가상 진입 기록 1건' in text and '입력 없음은 미매수 증명이 아니에요' in text
    assert '900099' not in text and '실제 매수 여부 미확인' in text
    second=report(tmp_path,'REAL_QUOTES',DAY,now=NOW,page=2)
    assert '900007' in second and '900001' not in second and '다음:' not in second
    assert before==(actual.read_bytes(),tracker.db.path.read_bytes())
    assert len(tracker.db.trades())==1


def test_missing_actual_file_does_not_mean_not_bought_or_create_a_database(tmp_path):
    tracker,_=setup(tmp_path)
    text=report(tmp_path,'REAL_QUOTES',DAY,now=NOW)
    assert '실제 매수 입력 미확인' in text and '체결 입력 파일 없음' in text
    assert not (tmp_path/'actual_fill_records.db').exists()
    assert '2페이지까지' in report(tmp_path,'REAL_QUOTES',DAY,now=NOW,page=3)
    assert '목록이 없어요' in report(tmp_path,'REAL_QUOTES','2026-10-19',now=NOW)


@pytest.mark.parametrize('damage',['payload','identity','date'])
def test_corrupt_actual_input_is_not_silently_treated_as_unbought(tmp_path,damage):
    setup(tmp_path);actual=tmp_path/'actual_fill_records.db'
    import_csv(actual,blob(row(filled_at=DAY+'T09:00:00+09:00')),now=NOW)
    with sqlite3.connect(actual) as db:
        if damage=='payload':db.execute("UPDATE actual_fills SET payload='PRIVATE_INVALID_PAYLOAD'")
        elif damage=='identity':db.execute("UPDATE actual_meta SET kind='OTHER'")
        else:db.execute("UPDATE actual_fills SET id='different-id'")
    with pytest.raises(ValueError,match='매수 여부를 추측하지') as error:
        report(tmp_path,'REAL_QUOTES',DAY,now=NOW)
    assert 'PRIVATE_INVALID_PAYLOAD' not in str(error.value)


def test_other_modes_future_dates_and_invalid_pages_do_not_read_actual_data(tmp_path):
    (tmp_path/'actual_fill_records.db').write_bytes(b'PRIVATE_INVALID_DATA')
    for mode in ('DEMO','MOCK'):
        assert '실제 체결을 연결하지' in report(tmp_path,mode,DAY,now=NOW)
    for day,page in [('2026-10-21',1),(DAY,0),(DAY,True)]:
        with pytest.raises(ValueError):report(tmp_path,'REAL_QUOTES',day,now=NOW,page=page)


def test_private_telegram_report_is_allowed_after_stop_and_never_orders(tmp_path,sandbox):
    tracker,cfg=setup(tmp_path);service,broker,_,clock=sandbox;clock[0]=NOW
    service.morning_setup=lambda text:setup_text(tmp_path,None,None,'REAL_QUOTES',cfg,text,now=NOW)
    bad=update(sandbox,1,'/추천대조');bad['message']['from']['id']=99
    assert service.handle(bad) is None
    service.handle(update(sandbox,2,'/중지'))
    text=service.handle(update(sandbox,3,'/추천대조'))
    assert '[추천과 매수 입력 비교]' in text
    assert '사용법' in service.handle(update(sandbox,4,'/추천대조 2026-10-20 0'))
    assert broker.calls==[]
