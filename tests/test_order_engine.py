"""No network or credentials: fake-exchange lifecycle and money invariants."""
from dataclasses import replace
from datetime import date, datetime, time, timezone
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from app.order_engine import OrderEngine, OrderRequest, OrderError, OrderRejected, connect
from app.simulated_broker import SimulatedBroker
from app.trading_clock import TradingCalendar, KST

DAY = date(2026, 10, 19)  # Explicit synthetic open day, not a live calendar feed.


@pytest.fixture
def rig(tmp_path):
    broker = SimulatedBroker(tmp_path/'fake.db', 100000)
    engine = OrderEngine(tmp_path/'journal.db', broker, TradingCalendar({DAY}),
                         lambda: datetime(2026, 10, 19, 10, tzinfo=KST))
    return engine, broker


def buy(quantity=5, price=1000):
    return OrderRequest('900001', 'BUY', quantity, price)


def test_buy_partial_amend_cancel_sell_and_balances(rig):
    engine, broker = rig
    order = engine.submit('buy', buy())
    assert order['state'] == 'OPEN' and order['filled_qty'] == 0
    assert engine.account() == broker.account()
    broker.fill(order['id'], 2, 900)
    assert engine.reconcile(order['id'])['state'] == 'PARTIAL'
    assert engine.account() == {'cash': 98200, 'positions': {'900001': 2}}
    # Replay a cumulative fill snapshot without charging cash a second time.
    engine.reconcile(order['id'])
    assert engine.account() == broker.account()
    amended = engine.amend('amend', order['id'], 4, 950)
    assert amended['quantity'] == 4 and amended['filled_qty'] == 2
    canceled = engine.cancel('cancel', order['id'])
    assert canceled['state'] == 'CANCELED' and canceled['filled_qty'] == 2
    sell = engine.submit('sell', OrderRequest('900001', 'SELL', 2, 1100))
    broker.fill(sell['id'], 1, 1200)
    engine.reconcile(sell['id'])
    broker.fill(sell['id'], 1, 1100)
    assert engine.reconcile(sell['id'])['state'] == 'FILLED'
    assert engine.account() == broker.account() == {'cash': 100500, 'positions': {}}


def test_idempotency_and_reused_key_with_different_payload(rig):
    engine, broker = rig
    order = engine.submit('same', buy())
    assert engine.submit('same', buy()) == order
    assert broker.calls == ['submit']
    with pytest.raises(OrderError, match='다른 주문'):
        engine.submit('same', buy(2))
    engine.amend('edit', order['id'], 6, 1000)
    engine.amend('edit', order['id'], 6, 1000)
    engine.cancel('cancel', order['id'])
    engine.cancel('cancel', order['id'])
    assert broker.calls == ['submit', 'amend', 'cancel']


def test_unknown_response_blocks_mutations_then_lookup_recovers(rig):
    engine, broker = rig
    broker.lose_next_response = True
    order = engine.submit('lost', buy())
    assert order['state'] == 'UNKNOWN'
    assert engine.submit('lost', buy())['id'] == order['id']
    with pytest.raises(OrderError, match='불명확'):
        engine.submit('another', buy())
    broker.fill(order['id'], 1, 950)
    assert engine.reconcile(order['id'])['state'] == 'PARTIAL'
    assert broker.calls.count('submit') == 1
    assert engine.account() == broker.account()
    engine.cancel('cancel', order['id'])


@pytest.mark.parametrize('kind', ['AMEND', 'CANCEL'])
def test_uncertain_amend_and_cancel_are_not_resent(rig, kind):
    engine, broker = rig
    order = engine.submit('buy', buy())
    broker.lose_next_response = True
    if kind == 'AMEND':
        order = engine.amend('action', order['id'], 6, 900)
    else:
        order = engine.cancel('action', order['id'])
    assert order['state'] == 'UNKNOWN'
    restored = engine.reconcile(order['id'])
    assert restored['state'] == ('OPEN' if kind == 'AMEND' else 'CANCELED')
    assert broker.calls.count(kind.lower()) == 1


def test_restart_preserves_journal_fake_account_and_fill_totals(rig):
    engine, broker = rig
    order = engine.submit('buy', buy())
    broker.fill(order['id'], 3, 1000)
    engine.reconcile(order['id'])
    restored_broker = SimulatedBroker(broker.path)
    restored = OrderEngine(engine.path, restored_broker, engine.calendar, engine.clock)
    restored.recover()
    assert restored.submit('buy', buy())['id'] == order['id']
    assert restored_broker.calls == []
    assert restored.account() == restored_broker.account()
    restored.reconcile(order['id'])
    assert restored.account()['cash'] == 97000


def test_crash_before_send_stays_unknown_no_automatic_resubmit(rig):
    engine, broker = rig
    def crashed(*args):
        raise KeyboardInterrupt('simulated process crash')
    broker.submit = crashed
    with pytest.raises(KeyboardInterrupt):
        engine.submit('crash', buy())
    engine.recover()
    with connect(engine.path) as db:
        order_id = db.execute('SELECT id FROM orders').fetchone()[0]
    assert engine.reconcile(order_id)['state'] == 'UNKNOWN'
    with pytest.raises(OrderError, match='불명확'):
        engine.submit('new', buy())


def test_reservations_prevent_overspending_and_overselling(rig):
    engine, broker = rig
    first = engine.submit('reserve', buy(90))
    with pytest.raises(OrderError, match='잔액'):
        engine.submit('too-much', buy(11))
    with pytest.raises(OrderError):
        engine.amend('too-large', first['id'], 101, 1000)
    engine.cancel('release', first['id'])
    filled = engine.submit('buy', buy(3))
    broker.fill(filled['id'], 3, 1000)
    engine.reconcile(filled['id'])
    sell = engine.submit('sell', OrderRequest('900001', 'SELL', 2, 1000))
    with pytest.raises(OrderError, match='보유 수량'):
        engine.submit('oversell', OrderRequest('900001', 'SELL', 2, 1000))
    assert sell['state'] == 'OPEN'


def test_concurrent_duplicate_only_calls_broker_once(rig):
    engine, broker = rig
    entered, release = Event(), Event()
    original = broker.submit
    def slow(*args):
        entered.set()
        assert release.wait(3)
        return original(*args)
    broker.submit = slow
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(engine.submit, 'same', buy())
        assert entered.wait(3)
        second = pool.submit(engine.submit, 'same', buy())
        duplicate = second.result(timeout=3)
        release.set()
        result = first.result(timeout=3)
    assert result['id'] == duplicate['id'] and broker.calls == ['submit']


def test_bad_fill_never_changes_ledger_and_blocks_new_order(rig):
    engine, broker = rig
    order = engine.submit('buy', buy())
    original = broker.lookup(order['id'])
    broker.lookup = lambda ref: replace(original, filled_qty=99, state='FILLED')
    before = engine.account()
    assert engine.reconcile(order['id'])['state'] == 'UNKNOWN'
    assert engine.account() == before
    with pytest.raises(OrderError, match='불명확'):
        engine.submit('new', buy())


def test_broker_rejection_is_not_a_fill_or_unknown(rig):
    engine, broker = rig
    def reject(*args):
        raise OrderRejected('synthetic rejection')
    broker.submit = reject
    assert engine.submit('reject', buy())['state'] == 'REJECTED'
    assert engine.account()['cash'] == 100000


def test_non_simulated_adapter_refused_before_any_call(tmp_path):
    class Forbidden:
        environment = 'REAL'
        def account(self):
            pytest.fail('must never call a non-simulated adapter')
    with pytest.raises(OrderError, match='오프라인'):
        OrderEngine(tmp_path/'never.db', Forbidden(), TradingCalendar())


@pytest.mark.parametrize('quantity,price', [(0, 100), (-1, 100), (True, 100), (1, 0), (1, 1.5)])
def test_invalid_order_has_no_side_effect(rig, quantity, price):
    engine, broker = rig
    with pytest.raises(OrderError):
        engine.submit('invalid', buy(quantity, price))
    assert broker.calls == []


def test_calendar_boundaries_unknown_days_and_special_schedule():
    calendar = TradingCalendar({DAY})
    at = lambda h, m=0, s=0: datetime(2026, 10, 19, h, m, s, tzinfo=KST)
    assert not calendar.is_open(at(8, 59), 'KRX')
    assert calendar.is_open(at(9), 'KRX')
    assert not calendar.is_open(at(15, 30), 'KRX')
    assert calendar.is_open(at(8), 'NXT')
    assert not calendar.is_open(at(8, 50), 'NXT')
    assert not calendar.is_open(at(9), 'NXT')
    assert calendar.is_open(at(9, 0, 30), 'NXT')
    assert not calendar.is_open(at(15, 20), 'NXT')
    assert calendar.is_open(at(15, 40), 'NXT')
    assert not calendar.is_open(at(20), 'NXT')
    assert calendar.is_open(at(9).astimezone(timezone.utc), 'KRX')
    assert not calendar.is_open(datetime(2026, 10, 20, 10, tzinfo=KST), 'KRX')
    calendar.overrides[(DAY, 'KRX')] = ((time(10), time(15, 30)),)
    assert not calendar.is_open(at(9), 'KRX')
    with pytest.raises(ValueError):
        calendar.is_open(datetime(2026, 10, 19, 10), 'KRX')


def test_closed_session_never_sends_or_schedules_order(rig):
    engine, broker = rig
    engine.clock = lambda: datetime(2026, 10, 17, 10, tzinfo=KST)
    with pytest.raises(OrderError, match='거래일'):
        engine.submit('weekend', buy())
    assert broker.calls == []


def test_demo_is_self_contained_and_restarts_cleanly():
    from app.order_demo import run_order_demo
    result = run_order_demo()
    assert result['mode'] == 'OFFLINE_SIMULATED'
    assert result['cash'] == 100400 and not result['positions']
