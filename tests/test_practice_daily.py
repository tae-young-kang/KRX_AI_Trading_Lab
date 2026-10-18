from app.config import DEFAULTS
from app.market import Stock
from app.storage import Database
from app.practice_daily import summarize


def test_partial_sales_on_different_days_are_not_two_completed_wins(tmp_path,monkeypatch):
    db=Database(tmp_path/'practice.db')
    monkeypatch.setattr('app.storage.now',lambda:'2026-10-19T10:00:00+09:00')
    stock=Stock('900001','연습','KOSPI',1000,3,200,150,4000)
    db.paper_open(stock,10,2,4,2,0,0,dict(DEFAULTS),3)
    tid=db.open_trades()[0]['id']
    first=db.paper_sell(tid,4,1100,'일부',0,0,0)
    with db.connect() as record: one=summarize(record,'2026-10-19')
    assert one['realized']==first==400 and one['wins']==0 and one['sale_count']==1
    monkeypatch.setattr('app.storage.now',lambda:'2026-10-20T00:30:00+09:00')
    second=db.paper_close(tid,900,'나머지',0,0,0)
    with db.connect() as record: two=summarize(record,'2026-10-20')
    assert two['realized']==second==-600 and two['losses']==1 and two['buy_count']==0
    assert two['sell_gross']==5400 and two['winrate']==0
    assert '당일 매도분 실현손익 -600.00원' in db.practice_daily_report('2026-10-20')


def test_empty_day_and_legacy_complete_do_not_invent_sale_cash(tmp_path,monkeypatch):
    db=Database(tmp_path/'practice.db')
    assert '승률 미확인' in db.practice_daily_report('2026-10-19')
    monkeypatch.setattr('app.storage.now',lambda:'2026-10-19T10:00:00+09:00')
    db.paper_open(Stock('900001','이전 연습','KOSPI',1000,3,200,150,4000),1,2,4,2,0,0,dict(DEFAULTS),3)
    db.paper_close(db.open_trades()[0]['id'],1100,'이전 완료',0,0,0)
    with db.connect() as record:
        record.execute('DELETE FROM paper_exits')
    with db.connect() as record: result=summarize(record,'2026-10-19')
    assert result['legacy']==1 and result['sell_gross']==0 and result['realized']==0 and result['wins']==0


def test_daily_symbol_totals_and_paging_match_whole_day(tmp_path,monkeypatch):
    db=Database(tmp_path/'practice.db')
    monkeypatch.setattr('app.storage.now',lambda:'2026-10-19T10:00:00+09:00')
    for i in range(6):
        db.paper_open(Stock(f'{900000+i:06}','연습'+str(i),'KOSPI',1000,3,200,150,4000),2,2,4,2,0,0,dict(DEFAULTS),10)
    for row in db.open_trades(): db.paper_sell(row['id'],1,1100,'일부 매도',0,0,0)
    with db.connect() as record: result=summarize(record,'2026-10-19')
    for field in ('buy_gross','sell_gross','realized'):
        assert sum(row[field] for row in result['symbols'])==result[field]
    first=db.practice_daily_report('2026-10-19');second=db.practice_daily_report('2026-10-19',2)
    assert '1/2페이지' in first and '/연습결산 2026-10-19 2' in first
    assert '900005' not in first and '900005' in second
    assert '실현손익 +600.00원' in first and '실현손익 +600.00원' in second
    assert '2페이지까지' in db.practice_daily_report('2026-10-19',3)


def test_recorded_take_reason_is_separate_from_net_win_and_counts_final_only(tmp_path,monkeypatch):
    db=Database(tmp_path/'practice.db')
    monkeypatch.setattr('app.storage.now',lambda:'2026-10-19T10:00:00+09:00')
    stock=Stock('900001','연습','KOSPI',1000,3,200,150,4000)
    db.paper_open(stock,2,2,4,2,1,0,dict(DEFAULTS),3)
    tid=db.open_trades()[0]['id']
    db.paper_sell(tid,1,1001,'가상 익절',1,1,0)
    with db.connect() as record: partial=summarize(record,'2026-10-19')
    assert partial['reasons']['take']==0 and partial['losses']==0
    db.paper_close(tid,1001,'가상 익절',1,1,0)
    with db.connect() as record: final=summarize(record,'2026-10-19')
    assert final['reasons']['take']==1 and final['losses']==1 and final['wins']==0
    assert '익절 1 / 손절 0' in db.practice_daily_report('2026-10-19')


import pytest

@pytest.mark.parametrize('table,column,value',[
    ('paper_exits','allocated_cost',1),('paper_exits','sell_fee',10),
    ('paper_exits','pnl',999),('paper_exits','qty',3),
    ('paper_trades','remaining_qty',1),('paper_trades','pnl',999),
    ('paper_trades','close_tax',10),('paper_trades','closed_at','2000-01-01T00:00:00+09:00')])
def test_daily_report_refuses_inconsistent_ledger_without_repair(tmp_path,monkeypatch,table,column,value):
    db=Database(tmp_path/'practice.db')
    monkeypatch.setattr('app.storage.now',lambda:'2026-10-19T10:00:00+09:00')
    db.paper_open(Stock('900001','연습','KOSPI',1000,3,200,150,4000),2,2,4,2,0,0,dict(DEFAULTS),3)
    db.paper_close(db.open_trades()[0]['id'],1100,'완료',0,0,0)
    with db.connect() as record:
        record.execute(f'UPDATE {table} SET {column}=?',(value,))
    before=db.trades()
    with pytest.raises(ValueError,match='장부'):
        db.practice_daily_report('2026-10-19')
    assert db.trades()==before
