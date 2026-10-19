from dataclasses import replace
from datetime import datetime,timedelta
import pytest
from app.mock_market_budget import UpperPrice,preview_buy
from app.kiwoom_mock_review import CapturedObservations
from app.protection_plan import ObservedPrice
from app.kiwoom_mock_contract import ContractError
from app.kiwoom_mock_transport import validated_plan
from app.trading_clock import KST
from test_kiwoom_mock_ledger import snapshot,TAG

NOW=datetime(2026,10,19,10,tzinfo=KST)


def inputs():
    capture=CapturedObservations(snapshot(), 'a'*32,'b'*32,NOW-timedelta(seconds=10),NOW)
    quote=ObservedPrice('MOCK','900001','KRX',1000,NOW,NOW)
    upper=UpperPrice('MOCK','900001','KRX',NOW.date(),1300,NOW)
    args=dict(code='900001',name='합성',market='KOSDAQ',profile_id='a'*32,auth_generation='b'*32,
              account_tag=TAG,now=NOW,fee_bps=2)
    return capture,quote,upper,args


def test_quantity_uses_upper_price_and_does_not_authorize_transport():
    capture,quote,upper,args=inputs()
    result=preview_buy(capture,quote,upper,quantity=10,**args)
    assert result['reserved_won']==13010 and result['targets'][0]['quantity']==10
    assert result['quote_won']==1000 and result['targets'][0]['market']=='KOSDAQ'
    assert result['available_won']==97010
    assert not result['executable'] and result['contract_draft'].body()['trde_tp']=='3'
    with pytest.raises(ContractError):validated_plan(result['contract_draft'])


def test_amount_floors_at_upper_price_and_insufficient_funds_has_no_contract():
    capture,quote,upper,args=inputs()
    result=preview_buy(capture,quote,upper,budget_won=10000,**args)
    assert result['targets'][0]['quantity']==7 and result['unreserved_won']==893
    no_funds=preview_buy(capture,quote,upper,quantity=100,**args)
    assert no_funds['state']=='INSUFFICIENT_FUNDS' and no_funds['contract_draft'] is None
    below=preview_buy(capture,quote,upper,budget_won=100,**args)
    assert below['state']=='BELOW_ONE_SHARE' and below['contract_draft'] is None


@pytest.mark.parametrize('field',['profile','generation','account','capture_stale','quote_stale','quote_future',
    'upper_stale','upper_day','upper_mode','quote_mode','venue','code','unfilled','price_above_upper'])
def test_mismatched_or_stale_evidence_is_rejected(field):
    capture,quote,upper,args=inputs()
    if field=='profile':args['profile_id']='c'*32
    elif field=='generation':args['auth_generation']='c'*32
    elif field=='account':args['account_tag']='c'*64
    elif field=='capture_stale':capture=replace(capture,started_at=NOW-timedelta(seconds=31))
    elif field=='quote_stale':quote=replace(quote,quote_at=NOW-timedelta(seconds=6))
    elif field=='quote_future':quote=replace(quote,quote_at=NOW+timedelta(seconds=1))
    elif field=='upper_stale':upper=replace(upper,observed_at=NOW-timedelta(seconds=6))
    elif field=='upper_day':upper=replace(upper,day=NOW.date()-timedelta(days=1))
    elif field=='upper_mode':upper=replace(upper,mode='REAL_QUOTES')
    elif field=='quote_mode':quote=replace(quote,mode='REAL_QUOTES')
    elif field=='venue':quote=replace(quote,venue='NXT')
    elif field=='code':upper=replace(upper,code='900002')
    elif field=='unfilled':capture=replace(capture,observations=replace(capture.observations,unfilled=(('0000012','900001',1,1),)))
    elif field=='price_above_upper':quote=replace(quote,price_won=1400)
    with pytest.raises(ContractError):preview_buy(capture,quote,upper,quantity=1,**args)


@pytest.mark.parametrize('intent',[{}, {'quantity':1,'budget_won':1000}, {'quantity':True},
    {'quantity':1.5},{'quantity':0},{'budget_won':True},{'budget_won':1.5},{'budget_won':0}])
def test_quantity_or_budget_must_be_exact_and_positive(intent):
    capture,quote,upper,args=inputs()
    with pytest.raises(ValueError):preview_buy(capture,quote,upper,**intent,**args)
