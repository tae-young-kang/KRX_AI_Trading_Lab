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
