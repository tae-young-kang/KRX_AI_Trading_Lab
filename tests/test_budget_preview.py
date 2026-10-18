import pytest
from app.budget_preview import equal_budget_preview


def targets(prices):
    return [dict(code=f'{900000+i:06}',name='연습',market='KOSPI',reference_won=p) for i,p in enumerate(prices)]


def test_equal_budget_floors_shares_and_keeps_remainder():
    result=equal_budget_preview(total_won=10000,targets=targets([1000,3000,4000]),available_won=10000,fee_bps=0,buffer_bps=0)
    assert [r['quantity'] for r in result['targets']]==[3,1,0]
    assert result['allocated_each_won']==3333 and result['reserved_won']==6000
    assert result['unreserved_won']==4000 and not result['executable']


def test_cost_and_buffer_are_explicit_and_conservative():
    result=equal_budget_preview(total_won=2000,targets=targets([1000]),available_won=2000,fee_bps=1,buffer_bps=1)
    assert result['targets'][0]['quantity']==1 and result['reserved_won']==1001


@pytest.mark.parametrize('cash,price,state',[(None,100,'FUNDS_UNKNOWN'),(999,100,'INSUFFICIENT_FUNDS'),(1000,None,'PRICE_MISSING')])
def test_unknown_or_short_funds_and_missing_prices_do_not_authorize(cash,price,state):
    result=equal_budget_preview(total_won=1000,targets=targets([price]),available_won=cash,fee_bps=0,buffer_bps=0)
    assert result['state']==state and result['executable'] is False


def test_duplicate_symbols_are_not_double_allocated():
    row=targets([100])[0]
    with pytest.raises(ValueError,match='중복'):
        equal_budget_preview(total_won=1000,targets=[row,row],available_won=1000,fee_bps=0,buffer_bps=0)


@pytest.mark.parametrize('amount',[True,-1,0,float('nan'),1000.5])
def test_invalid_budget_rejected(amount):
    with pytest.raises(ValueError):
        equal_budget_preview(total_won=amount,targets=targets([100]),available_won=1000,fee_bps=0,buffer_bps=0)
