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
