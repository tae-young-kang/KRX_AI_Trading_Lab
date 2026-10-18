from app.practice_details import sale_details


def test_split_sale_detail_is_read_only_and_hides_internal_requests():
    trade=dict(name='연습종목',remaining_qty=2,qty=3,pnl=None,closed_at=None,realized_pnl=88)
    sale=dict(sold_at='2026-10-18T12:00:00',qty=1,reason='일부 매도',reference=1100,
              fill=1099,allocated_cost=1000,sell_fee=10,sell_tax=1,pnl=88,
              request_key='hidden-internal',request_json='hidden-payload')
    text=sale_details(trade,[sale])
    assert '남은 수량 2주 / 처음 3주' in text
    assert '1번째 매도' in text and '매수 수수료 포함' in text
    assert '88.00원' in text and '실제 증권사 체결이 아니' in text
    assert 'hidden' not in text and trade['remaining_qty']==2


def test_legacy_and_unknown_money_are_not_invented():
    trade=dict(name='이전종목',remaining_qty=0,qty=3,pnl=None,closed_at='old',realized_pnl=0)
    text=sale_details(trade,[])
    assert '이전 버전' in text and '누적 실현손익 확인 필요' in text


def test_practice_sale_report_pages_and_mode_database_isolation(tmp_path):
    from app.storage import Database
    from app.market import Stock
    from app.config import DEFAULTS
    db=Database(tmp_path/'one.db')
    st=Stock('900001','연습종목','KOSPI',1000,3,200,150,4000)
    db.paper_open(st,3,2,4,2,0,0,dict(DEFAULTS),3)
    tid=db.open_trades()[0]['id']
    db.paper_sell(tid,1,1100,'일부',0,0,0)
    db.paper_close(tid,1200,'나머지',0,0,0)
    assert f'{tid}: 연습종목' in db.practice_sale_report()
    first=db.practice_sale_report(tid)
    second=db.practice_sale_report(tid,2)
    assert '1/2페이지' in first and f'/연습내역 {tid} 2' in first
    assert '2번째 매도' in second and '나머지' in second
    assert '마지막 페이지' in db.practice_sale_report(tid,3)
    assert '없는 번호' in Database(tmp_path/'other.db').practice_sale_report(tid)


def test_practice_list_reaches_older_entries_and_empty_page_bounds(tmp_path):
    from app.storage import Database
    from app.market import Stock
    from app.config import DEFAULTS
    db=Database(tmp_path/'practice.db')
    assert '전체 0개' in db.practice_sale_report()
    for i in range(6):
        stock=Stock(f'{900000+i:06}','연습'+str(i),'KOSPI',1000,3,200,150,4000)
        db.paper_open(stock,1,2,4,2,0,0,dict(DEFAULTS),10)
    assert '목록 1/2페이지' in db.practice_sale_report()
    assert '/연습내역 목록 2' in db.practice_sale_report()
    second=db.practice_sale_report(None,2)
    assert '1: 연습0' in second and '6: 연습5' not in second
    assert '마지막 목록 페이지는 2' in db.practice_sale_report(None,3)
    assert '페이지 번호' in db.practice_sale_report(None,True)
