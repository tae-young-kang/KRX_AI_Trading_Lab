from dataclasses import replace
from datetime import datetime,timedelta,timezone
import pytest
from app.kiwoom_mock_contract import ContractError
from app.kiwoom_mock_ledger import MockFillLedger
from app.kiwoom_mock_review import CapturedObservations
from app.mock_protection import position_from_capture
from app.protection_plan import ObservedPrice,evaluate
from app.order_engine import connect
from test_kiwoom_mock_ledger import snapshot,FILL,LATER,TAG,DAY,BUY

NOW=datetime(2026,10,19,10,tzinfo=timezone(timedelta(hours=9)))


def setup(tmp_path):
    ledger=MockFillLedger(tmp_path/'fills.db',account_tag=TAG)
    observation=replace(snapshot(FILL,LATER),holdings=(('900001',3,3),))
    ledger.ingest(observation)
    capture=CapturedObservations(observation,'synthetic-profile','synthetic-generation',NOW,NOW)
    return ledger,capture


def test_persistent_fills_and_current_holdings_feed_non_executable_decision(tmp_path):
    ledger,capture=setup(tmp_path)
    before=ledger.project_order(DAY,'12',BUY)
    position=position_from_capture(ledger,capture,code='900001',buy_orders=('12',),now=NOW)
    assert position.filled_quantity==3 and position.filled_value_won==2980
    decision=evaluate(position,ObservedPrice('MOCK','900001','KRX',1100,NOW,NOW),now=NOW)
    assert decision.state=='TAKE_PROFIT' and decision.quantity==3 and not decision.executable
    assert ledger.project_order(DAY,'12',BUY)==before


def test_partial_sale_and_unattributed_same_symbol_fill(tmp_path):
    ledger,capture=setup(tmp_path)
    sale=replace(FILL,order_no='0000013',side='SELL',price=1100)
    obs=replace(snapshot(FILL,LATER,sale),holdings=(('900001',2,2),))
    ledger.ingest(obs)
    capture=replace(capture,observations=obs)
    with pytest.raises(ContractError):
        position_from_capture(ledger,capture,code='900001',buy_orders=('12',),now=NOW)
    position=position_from_capture(ledger,capture,code='900001',buy_orders=('12',),sell_orders=('13',),now=NOW)
    assert position.sold_quantity==1 and position.filled_quantity-position.sold_quantity==2


@pytest.mark.parametrize('target',['account','fills'])
def test_persistent_metadata_and_fill_rows_are_rechecked(tmp_path,target):
    ledger,capture=setup(tmp_path)
    with connect(ledger.path) as db:
        if target=='account': db.execute('UPDATE fill_meta SET account_tag=?',('b'*64,))
        else: db.execute('DELETE FROM fill_events')
    with pytest.raises(ContractError):
        position_from_capture(ledger,capture,code='900001',buy_orders=('12',),now=NOW)


@pytest.mark.parametrize('change', ['stale','account','holdings','unfilled','unknown_order','new_snapshot'])
def test_inconsistent_or_stale_evidence_is_blocked(tmp_path,change):
    ledger,capture=setup(tmp_path)
    orders=('12',)
    if change=='stale': capture=replace(capture,started_at=NOW-timedelta(seconds=31))
    elif change=='account': capture=replace(capture,observations=replace(capture.observations,account_tag='b'*64))
    elif change=='unknown_order': orders=('12','13')
    else:
        obs=capture.observations
        if change=='holdings': obs=replace(obs,holdings=(('900001',3,2),))
        elif change=='unfilled': obs=replace(obs,unfilled=(('0000012','900001',4,1),))
        else: obs=replace(obs,holdings=(('900001',4,4),))
        ledger.ingest(obs)
        if change!='new_snapshot': capture=replace(capture,observations=obs)
    with pytest.raises(ContractError):
        position_from_capture(ledger,capture,code='900001',buy_orders=orders,now=NOW)
