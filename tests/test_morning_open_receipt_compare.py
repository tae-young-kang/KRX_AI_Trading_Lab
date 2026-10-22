"""Synthetic CSV/user-claim comparison: never an exchange certificate or fill."""
from dataclasses import replace
from datetime import datetime
import csv

import pytest

from app.morning_open_receipt_compare import compare_saved, read_receipts, render
from app.morning_virtual import record_path
from app.trading_clock import KST
from test_morning_venue_policy import new
from test_morning_virtual import trade

DAY = '2026-10-20'
NOW = datetime(2026, 10, 22, 11, 0, tzinfo=KST)
HEADER = ('day','market','code','venue','first_at','price_won',
          'evidence_hash','capture_status')


def receipt_file(tmp_path, rows):
    path = tmp_path / 'user_claim.csv'
    with path.open('w',encoding='utf-8-sig',newline='') as fp:
        wr = csv.DictWriter(fp,fieldnames=list(HEADER))
        wr.writeheader()
        wr.writerows(rows)
    return path


def row(*,code='900001',market='KOSPI',price='1000',capture='CLAIMED_COMPLETE'):
    return dict(day=DAY,market=market,code=code,venue='KRX',
                first_at='2026-10-20T09:00:00+09:00',
                price_won=price,evidence_hash='a'*64,
                capture_status=capture)


def first_entry(tracker):
    start = datetime(2026,10,20,9,0,tzinfo=KST)
    supplied = replace(trade(venue='KRX',hour=9),at=start,fetched_at=start)
    assert tracker.enter(supplied,now=start)['state']=='VIRTUAL_ENTERED'


def test_matching_user_claim_is_not_certified_first_trade_or_actual_fill(tmp_path):
    tracker,_=new(tmp_path)
    first_entry(tracker)
    receipt=receipt_file(tmp_path,[row()])
    existing=record_path(tmp_path,'DEMO',DAY).read_bytes()
    status=compare_saved(tmp_path,'DEMO',DAY,receipt,now=NOW)
    assert status['counts']['MATCHED_USER_CLAIM']==1
    assert status['counts']['NO_RECEIPT']==2
    result=render(status)
    assert '사용자 주장' in result and '인증서가 아닙니다' in result
    assert '실제 주문·수익률은 증명되지 않았어요' in result
    assert record_path(tmp_path,'DEMO',DAY).read_bytes()==existing
    assert len(tracker.db.trades())==1


def test_no_virtual_open_does_not_create_fill_for_user_supplied_receipt(tmp_path):
    tracker,_=new(tmp_path)
    receipt=receipt_file(tmp_path,[row()])
    status=compare_saved(tmp_path,'DEMO',DAY,receipt,now=NOW)
    assert status['counts']['NO_VIRTUAL_ENTRY']==1
    assert status['counts']['NO_RECEIPT']==2
    assert tracker.db.trades()==[]


def test_price_hash_or_timestamp_conflict_remains_visible(tmp_path):
    tracker,_=new(tmp_path)
    first_entry(tracker)
    changed=row(price='1001')
    path=receipt_file(tmp_path,[changed])
    assert compare_saved(tmp_path,'DEMO',DAY,path,now=NOW)['counts']['RECEIPT_MISMATCH']==1
    changed=row()
    changed['evidence_hash']='b'*64
    path=receipt_file(tmp_path,[changed])
    assert compare_saved(tmp_path,'DEMO',DAY,path,now=NOW)['counts']['RECEIPT_MISMATCH']==1
    changed=row()
    changed['first_at']='2026-10-20T09:01:00+09:00'
    path=receipt_file(tmp_path,[changed])
    assert compare_saved(tmp_path,'DEMO',DAY,path,now=NOW)['counts']['RECEIPT_MISMATCH']==1


def test_matching_values_without_session_completeness_are_not_first_trade_proof(tmp_path):
    tracker,_=new(tmp_path)
    first_entry(tracker)
    receipt=receipt_file(tmp_path,[row(capture='UNKNOWN')])
    report=compare_saved(tmp_path,'DEMO',DAY,receipt,now=NOW)
    assert report['counts']['USER_CAPTURE_UNVERIFIED']==1
    assert '수집 완전성 불명 1' in render(report)


@pytest.mark.parametrize('field,value',[
    ('venue','NXT'),
    ('day','2026-10-21'),
    ('market','WRONG'),
    ('first_at','2026-10-20T08:59:00+09:00'),
    ('first_at','2030-10-20T09:00:00+09:00'),
    ('first_at','2026-10-20T09:00:00'),
    ('price_won','0'),
    ('price_won','=1000'),
    ('evidence_hash','invalid'),
    ('capture_status','CERTIFIED'),
])
def test_invalid_csv_is_rejected_without_importing_data(tmp_path,field,value):
    tracker,_=new(tmp_path)
    entry=row()
    entry[field]=value
    receipt=receipt_file(tmp_path,[entry])
    with pytest.raises(ValueError,match='CSV'):
        compare_saved(tmp_path,'DEMO',DAY,receipt,now=NOW)
    assert tracker.db.trades()==[]


def test_duplicate_symbol_or_outside_cohort_fails_closed(tmp_path):
    tracker,_=new(tmp_path)
    duplicate=receipt_file(tmp_path,[row(),row()])
    with pytest.raises(ValueError):
        compare_saved(tmp_path,'DEMO',DAY,duplicate,now=NOW)
    outside=receipt_file(tmp_path,[row(code='999999')])
    with pytest.raises(ValueError,match='목록 밖'):
        compare_saved(tmp_path,'DEMO',DAY,outside,now=NOW)
    assert tracker.db.trades()==[]


def test_missing_cohort_source_modes_and_legacy_are_not_backfilled(tmp_path):
    tracker,_=new(tmp_path)
    receipt=receipt_file(tmp_path,[row()])
    assert compare_saved(tmp_path,'MOCK',DAY,receipt,now=NOW) is None
    assert '고정된 아침 추천 목록이 없어' in render(None)
    assert not record_path(tmp_path,'MOCK',DAY).exists()
    with pytest.raises(ValueError):
        compare_saved(tmp_path,'DEMO','2030-10-20',receipt,now=NOW)


def test_large_invalid_csv_and_display_limit(tmp_path):
    tracker,_=new(tmp_path)
    receipt=receipt_file(tmp_path,[row()])
    receipt.write_bytes(b'X'*(1024*1024+1))
    with pytest.raises(ValueError):
        read_receipts(receipt,day=DAY,now=NOW)
    valid=receipt_file(tmp_path,[row()])
    with pytest.raises(ValueError):
        render(compare_saved(tmp_path,'DEMO',DAY,valid,now=NOW),max_rows=0)


def test_cli_and_gui_entry_are_explicit_read_only_paths():
    import inspect
    from app import morning_open_receipt_compare as module
    from app import gui
    function=inspect.getsource(module.main)
    gui_source=inspect.getsource(gui.MainWindow.compare_morning_open_csv)
    assert '--root' in function and '--csv' in function
    assert 'QFileDialog.getOpenFileName' in gui_source
    assert 'compare_saved(' in gui_source
    assert 'paper_open' not in gui_source and 'order' not in gui_source
