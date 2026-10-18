"""Hypothetical split sales on temporary practice DB; never real fills/orders."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
import csv
from datetime import datetime, timedelta, timezone
import pytest
from app.config import DEFAULTS
from app.market import Stock
from app.storage import Database
from app.trade_review import outcome


def position(tmp_path,quantity=10):
    db=Database(tmp_path/'practice.db')
    stock=Stock('900001','연습종목','KOSPI',1000,3,200,150,4000)
    db.paper_open(stock,quantity,2,4,2,1,0,deepcopy(DEFAULTS),3)
    return db,db.open_trades()[0]['id']


@pytest.mark.parametrize('bad_time', ['broken', None, '2000-01-01T00:00:00'])
def test_sale_time_validation_is_atomic(tmp_path,monkeypatch,bad_time):
    db,tid=position(tmp_path)
    before=db.trades()[0]
    monkeypatch.setattr('app.storage.now',lambda:bad_time)
    with pytest.raises(ValueError,match='시각'):
        db.paper_sell(tid,2,1100,'시각 검사',0,0,0)
    assert db.trades()[0]==before and db.paper_exit_history(tid)==[]


def test_clock_reversal_blocks_new_sale_but_replay_stays_idempotent(tmp_path,monkeypatch):
    db,tid=position(tmp_path)
    opened=datetime.fromisoformat(db.trades()[0]['opened_at']).astimezone()
    monkeypatch.setattr('app.storage.now',lambda:(opened+timedelta(seconds=10)).isoformat())
    first=db.paper_sell(tid,2,1100,'시각 검사',0,0,0,request_key='time-replay')
    before=db.trades()[0]
    monkeypatch.setattr('app.storage.now',lambda:(opened+timedelta(seconds=5)).isoformat())
    assert db.paper_sell(tid,2,1100,'시각 검사',0,0,0,request_key='time-replay')==first
    with pytest.raises(ValueError,match='시각'):
        db.paper_sell(tid,2,1100,'새 매도',0,0,0)
    assert db.trades()[0]==before and len(db.paper_exit_history(tid))==1
    monkeypatch.setattr('app.storage.now',lambda:(opened+timedelta(seconds=10)).astimezone(timezone.utc).isoformat())
    db.paper_close(tid,1100,'동일 순간 다른 offset',0,0,0)
    assert db.trades()[0]['remaining_qty']==0


def test_split_sales_allocate_entry_fee_once_and_count_one_final_outcome(tmp_path):
    db,tid=position(tmp_path)
    first=db.paper_sell(tid,4,1200,'일부 연습 매도',1,1,0,request_key='first-sale')
    assert first==pytest.approx(4800-48-48-4040)
    trade=db.trades()[0]
    assert trade['qty']==10 and trade['remaining_qty']==6 and trade['realized_pnl']==first
    assert trade['pnl'] is None and outcome(trade)=='보유 중'
    assert db.condition_reviews()[0]['total']['count']==0
    second=db.paper_close(tid,900,'나머지 연습 매도',1,1,0,request_key='final-sale')
    trade=db.trades()[0]
    assert trade['remaining_qty']==0 and trade['pnl']==pytest.approx(first+second)
    assert trade['close_fill']==pytest.approx(1020)
    assert trade['close_fee']==pytest.approx(102) and trade['close_tax']==pytest.approx(102)
    assert trade['pnl']==pytest.approx(10200-102-102-10100)
    assert outcome(trade)=='패' and db.stats()['count']==1
    assert db.condition_reviews()[0]['total']['losses']==1
    exits=db.paper_exit_history(tid)
    assert len(exits)==2 and sum(row['allocated_cost'] for row in exits)==pytest.approx(10100)
    assert json.loads(trade['exit_settings_json'])['paper_fee_pct']==1


def test_replay_is_idempotent_even_after_close_but_changed_request_is_rejected(tmp_path):
    db,tid=position(tmp_path)
    first=db.paper_sell(tid,3,1200,'시험',0,0,0,request_key='same-sale')
    assert db.paper_sell(tid,3,1200,'시험',0,0,0,request_key='same-sale')==first
    assert db.trades()[0]['remaining_qty']==7
    db.paper_close(tid,1100,'마지막',0,0,0)
    assert db.paper_sell(tid,3,1200,'시험',0,0,0,request_key='same-sale')==first
    with pytest.raises(ValueError,match='다른 내용'):
        db.paper_sell(tid,4,1200,'시험',0,0,0,request_key='same-sale')
    assert len(db.paper_exit_history(tid))==2


@pytest.mark.parametrize('quantity,price,fee,tax,slip',[(0,1000,0,0,0),(11,1000,0,0,0),
    (True,1000,0,0,0),(1,float('nan'),0,0,0),(1,1000,-1,0,0),(1,1000,0,100,0),
    (1,1000,0,0,100),(1,1e308,0,0,0),(1,1000,70,40,0)])
def test_invalid_or_overflowing_exit_never_changes_remaining_or_history(tmp_path,quantity,price,fee,tax,slip):
    db,tid=position(tmp_path)
    if price==1e308: quantity=10
    before=db.trades()[0]
    with pytest.raises((ValueError,OverflowError)):
        db.paper_sell(tid,quantity,price,'시험',fee,tax,slip)
    assert db.trades()[0]==before and db.paper_exit_history(tid)==[]


def test_two_competing_sales_cannot_oversell(tmp_path):
    db,tid=position(tmp_path)
    def attempt(key):
        try: return db.paper_sell(tid,7,1100,'동시 시험',0,0,0,request_key=key)
        except ValueError: return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(attempt,('parallel-one','parallel-two')))
    assert sum(result is not None for result in results)==1
    assert db.trades()[0]['remaining_qty']==3 and len(db.paper_exit_history(tid))==1


def test_restart_preserves_partial_sale_and_detects_inconsistent_remaining(tmp_path):
    db,tid=position(tmp_path)
    db.paper_sell(tid,2,1100,'시험',0,0,0)
    reopened=Database(db.path)
    assert reopened.trades()[0]['remaining_qty']==8 and len(reopened.paper_exit_history(tid))==1
    with reopened.connect() as record:
        record.execute('UPDATE paper_trades SET remaining_qty=9 WHERE id=?',(tid,))
    with pytest.raises(ValueError,match='대조해야'):
        reopened.paper_close(tid,1200,'잘못된 잔량',0,0,0)
    assert len(reopened.paper_exit_history(tid))==1


def test_legacy_migration_preserves_rows_without_inventing_exit_events(tmp_path):
    db,tid=position(tmp_path)
    db.paper_close(tid,1100,'시험',0,0,0)
    with db.connect() as record:
        original=dict(record.execute('SELECT * FROM paper_trades').fetchone())
        record.execute('DROP TABLE paper_exits')
        record.execute('ALTER TABLE paper_trades DROP COLUMN remaining_qty')
        record.execute('ALTER TABLE paper_trades DROP COLUMN realized_pnl')
    reopened=Database(db.path)
    trade=reopened.trades()[0]
    assert trade['pnl']==original['pnl'] and trade['remaining_qty']==0
    assert trade['realized_pnl']==original['pnl'] and reopened.paper_exit_history(tid)==[]


def test_partial_realized_money_is_visible_but_winrate_waits_for_full_exit(tmp_path):
    db,tid=position(tmp_path)
    first=db.paper_sell(tid,3,1200,'일부',0,0,0)
    stats=db.stats()
    assert stats['pnl']==first and stats['partial_pnl']==first and stats['closed_pnl']==0
    assert stats['count']==0 and stats['open_count']==1 and stats['wins']==0
    db.paper_close(tid,900,'나머지',0,0,0)
    stats=db.stats()
    assert stats['partial_pnl']==0 and stats['closed_pnl']==stats['pnl']
    assert stats['count']==1 and stats['open_count']==0


def test_mixed_exit_cost_assumptions_preserve_final_outcome_but_never_qualify(tmp_path):
    db,tid=position(tmp_path)
    db.paper_sell(tid,4,1200,'일부',0,0,0)
    db.paper_close(tid,1200,'나머지',1,1,0)
    trade=db.trades()[0]
    assert json.loads(trade['exit_settings_json'])['mixed'] is True
    review=db.condition_reviews()[0]
    assert review['total']['count']==1 and review['total']['wins']==1
    assert review['cost_status']=='MIXED' and review['exit_costs'] is None
    assert not review['eligible'] and review['state']=='분할매도 비용 가정 다름'


def test_aggregate_and_segment_exports_keep_remaining_costs_and_hide_request_keys(tmp_path):
    db,tid=position(tmp_path)
    db.paper_sell(tid,2,1200,'일부',1,1,0,request_key='private-operation')
    aggregate=tmp_path/'trades.csv';segments=tmp_path/'sales.csv'
    db.export_trades(aggregate);db.export_paper_exits(segments)
    with aggregate.open(encoding='utf-8-sig') as stream: row=list(csv.DictReader(stream))[0]
    assert row['qty']=='10' and row['remaining_qty']=='8' and row['outcome']=='보유 중'
    assert float(row['realized_pnl'])==pytest.approx(2400-24-24-2020)
    with segments.open(encoding='utf-8-sig') as stream: sale=list(csv.DictReader(stream))[0]
    assert sale['qty']=='2' and sale['trade_id']==str(tid)
    assert 'request_key' not in sale and 'request_json' not in sale
    assert 'private-operation' not in segments.read_text(encoding='utf-8-sig')


def test_unknown_legacy_pnl_is_excluded_instead_of_counted_as_break_even(tmp_path):
    db,tid=position(tmp_path)
    with db.connect() as record:
        record.execute("UPDATE paper_trades SET closed_at='2026-10-19T12:00:00',pnl=NULL,remaining_qty=0 WHERE id=?",(tid,))
    stats=db.stats()
    assert stats['unknown_count']==1 and stats['count']==0 and stats['wins']==0
