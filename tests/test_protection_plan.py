from dataclasses import replace
from datetime import datetime,timedelta,timezone
import pytest
from app.protection_plan import FilledPosition,ObservedPrice,evaluate

NOW=datetime(2026,10,19,10,tzinfo=timezone(timedelta(hours=9)))
POSITION=FilledPosition('MOCK','900001','KRX',10,10000,reconciled=True)
QUOTE=ObservedPrice('MOCK','900001','KRX',1000,NOW,NOW)


@pytest.mark.parametrize('price,state', [(1019,'HOLD'),(1020,'TAKE_PROFIT'),(970,'STOP_LOSS'),(971,'HOLD'),(900,'STOP_LOSS')])
def test_exact_default_trigger_boundaries_are_not_orders(price,state):
    decision=evaluate(POSITION,replace(QUOTE,price_won=price),now=NOW)
    assert decision.state==state and decision.quantity==10 and not decision.executable


def test_partial_fills_sold_quantity_and_pending_sell():
    position=replace(POSITION,filled_quantity=3,filled_value_won=3001,sold_quantity=1)
    assert evaluate(position,replace(QUOTE,price_won=1020),now=NOW).state=='HOLD'
    decision=evaluate(position,replace(QUOTE,price_won=1021),now=NOW)
    assert decision.state=='TAKE_PROFIT' and decision.quantity==2
    assert evaluate(replace(position,pending_sell_quantity=1),QUOTE,now=NOW).state=='BLOCKED'
    assert evaluate(replace(position,sold_quantity=3),QUOTE,now=NOW).state=='NO_POSITION'
    empty=replace(POSITION,filled_quantity=0,filled_value_won=0)
    assert evaluate(empty,QUOTE,now=NOW).state=='NO_POSITION'


@pytest.mark.parametrize('change', [dict(reconciled=False),dict(filled_quantity=True),dict(filled_value_won=-1),
    dict(sold_quantity=11),dict(pending_sell_quantity=11),dict(filled_value_won=0),dict(mode='LIVE')])
def test_uncertain_or_invalid_fill_evidence_blocks(change):
    assert evaluate(replace(POSITION,**change),QUOTE,now=NOW).state=='BLOCKED'


@pytest.mark.parametrize('change', [dict(mode='REAL_QUOTES'),dict(code='900002'),dict(venue='NXT'),
    dict(price_won=True),dict(quote_at=NOW-timedelta(seconds=31)),
    dict(fetched_at=NOW-timedelta(seconds=31)),dict(quote_at=NOW+timedelta(seconds=1)),
    dict(quote_at=NOW.replace(tzinfo=None)),dict(fetched_at=NOW-timedelta(seconds=1))])
def test_mixed_stale_future_and_naive_quotes_block(change):
    assert evaluate(POSITION,replace(QUOTE,**change),now=NOW).state=='BLOCKED'


def test_custom_policy_and_utc_time_are_exact():
    quote=replace(QUOTE,price_won=1040,quote_at=NOW.astimezone(timezone.utc))
    assert evaluate(POSITION,quote,now=NOW,take_bps=400,stop_bps=100).state=='TAKE_PROFIT'
    with pytest.raises(ValueError): evaluate(POSITION,quote,now=NOW,stop_bps=10000)
