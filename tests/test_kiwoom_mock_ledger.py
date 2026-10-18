"""Synthetic fill/cost evidence in temporary DBs; no broker or network calls."""
from dataclasses import replace
from datetime import date

import pytest

from app.kiwoom_mock_contract import ContractError
from app.kiwoom_mock_ledger import MockFillLedger
from app.kiwoom_mock_review import Observations, Execution, FeeObservation
from app.order_engine import OrderRequest, connect

DAY = date(2026, 10, 19)
TAG = 'a' * 64  # Synthetic opaque tag.
BUY = OrderRequest('900001', 'BUY', 3, 1000)
FILL = Execution('0000012', '0000001', None, '900001', 'BUY', 1, 1000)
LATER = replace(FILL, fill_no='0000002', quantity=2, price=990)


def snapshot(*items, day=DAY):
    return Observations(day, TAG,
        (('entr', 100000), ('ord_alow_amt', 97010), ('100stk_ord_alow_amt', 97010),
         ('d1_entra', 100000), ('d2_entra', 97010)),
        (('900001', sum(item.quantity for item in items), 0),), tuple(items),
        (FeeObservation('0000012', '900001', 10, 0),), ())


def confirm(ledger, *, day=DAY, order='0000012', quantity=1, gross=1000,
            commission=10, tax=0, evidence='b' * 64, item='line-1', source='SYNTHETIC_TEST'):
    ledger.confirm_costs(day, order, commission=commission, tax=tax,
        filled_quantity=quantity, gross=gross, source=source,
        evidence_hash=evidence, evidence_item=item)


def test_restart_duplicate_and_new_fills_invalidate_old_costs(tmp_path):
    path = tmp_path/'fills.db'
    ledger = MockFillLedger(path, account_tag=TAG)
    assert ledger.ingest(snapshot(FILL)) == 1
    first = ledger.project_order(DAY, '12', BUY)
    assert first.fill_state == 'PARTIAL_FILL_OBSERVED'
    assert first.commission is None and first.net_cash_flow is None
    confirm(ledger)
    assert ledger.project_order(DAY, '12', BUY).net_cash_flow == -1010
    ledger = MockFillLedger(path, account_tag=TAG)
    confirm(ledger)  # Identical evidence is idempotent.
    assert ledger.ingest(snapshot(FILL)) == 0
    assert ledger.ingest(snapshot(LATER, FILL)) == 1
    full = ledger.project_order(DAY, '0000012', BUY)
    assert (full.filled_qty, full.filled_value) == (3, 2980)
    assert full.fill_state == 'FULL_FILL_OBSERVED'
    assert full.net_cash_flow is None and full.commission is None
    confirm(ledger, quantity=3, gross=2980, evidence='c' * 64, commission=30)
    full = ledger.project_order(DAY, '12', BUY)
    assert full.net_cash_flow == -3010 and full.cost_source == 'SYNTHETIC_TEST'
    assert full.ready_for_orders is False


def test_missing_or_changed_old_fill_rolls_back_entire_snapshot(tmp_path):
    ledger = MockFillLedger(tmp_path/'fills.db', account_tag=TAG)
    ledger.ingest(snapshot(FILL, LATER))
    for changed in (snapshot(FILL), snapshot(replace(FILL, price=999), LATER)):
        with pytest.raises(ContractError):
            ledger.ingest(changed)
    assert ledger.project_order(DAY, '12', BUY).filled_value == 2980
    with connect(ledger.path) as db:
        assert db.execute('SELECT count(*) FROM fill_events').fetchone()[0] == 2


def test_order_numbers_are_scoped_by_day_and_account(tmp_path):
    path = tmp_path/'fills.db'
    ledger = MockFillLedger(path, account_tag=TAG)
    ledger.ingest(snapshot(FILL))
    tomorrow = date(2026, 10, 20)
    ledger.ingest(snapshot(replace(FILL, price=900), day=tomorrow))
    assert ledger.project_order(DAY, '12', BUY).filled_value == 1000
    assert ledger.project_order(tomorrow, '12', BUY).filled_value == 900
    with pytest.raises(ContractError):
        MockFillLedger(path, account_tag='d' * 64)
    with pytest.raises(ContractError):
        ledger.ingest(replace(snapshot(FILL), account_tag='d' * 64))


def test_confirmed_costs_require_exact_totals_and_immutable_evidence(tmp_path):
    ledger = MockFillLedger(tmp_path/'fills.db', account_tag=TAG)
    ledger.ingest(snapshot(FILL))
    for args in (dict(quantity=2), dict(gross=999), dict(commission=-1),
                 dict(commission=True), dict(source='ka10076'), dict(evidence='unverified'),
                 dict(item='private/path.csv')):
        with pytest.raises(ContractError):
            confirm(ledger, **args)
    confirm(ledger)
    with pytest.raises(ContractError):
        confirm(ledger, commission=11)
    other = replace(FILL, order_no='0000013')
    ledger.ingest(snapshot(FILL, other))
    with pytest.raises(ContractError):
        confirm(ledger, order='13')
    confirm(ledger, order='13', item='line-2')


def test_sell_net_cash_flow_does_not_become_deposit(tmp_path):
    ledger = MockFillLedger(tmp_path/'fills.db', account_tag=TAG)
    sale = replace(FILL, side='SELL', price=1100)
    ledger.ingest(snapshot(sale))
    confirm(ledger, gross=1100, commission=10, tax=2)
    view = ledger.project_order(DAY, '12', OrderRequest('900001', 'SELL', 1, 1000))
    assert view.net_cash_flow == 1088 and view.ready_for_orders is False
    assert not hasattr(view, 'cash_available')


@pytest.mark.parametrize('order_request', [OrderRequest('900002', 'BUY', 3, 1000),
    OrderRequest('900001', 'SELL', 3, 1000), OrderRequest('900001', 'BUY', 3, 999),
    OrderRequest('900001', 'BUY', 1, 1000), OrderRequest('900001', 'BUY', 3, 1000, 'NXT')])
def test_read_only_engine_contract_rejects_mismatch(tmp_path, order_request):
    ledger = MockFillLedger(tmp_path/'fills.db', account_tag=TAG)
    ledger.ingest(snapshot(FILL, LATER))
    with pytest.raises(ContractError):
        ledger.project_order(DAY, '12', order_request)


def test_no_fill_evidence_is_neither_open_nor_canceled(tmp_path):
    ledger = MockFillLedger(tmp_path/'fills.db', account_tag=TAG)
    view = ledger.project_order(DAY, '12', BUY)
    assert view.fill_state == 'NO_FILL_EVIDENCE' and view.net_cash_flow is None
    with pytest.raises(ContractError):
        confirm(ledger)


def test_foreign_database_and_untrusted_snapshot_cannot_store_private_fields(tmp_path):
    path = tmp_path/'practice.db'
    with connect(path) as db:
        db.execute('CREATE TABLE practice(value INTEGER)')
    with pytest.raises(ContractError):
        MockFillLedger(path, account_tag=TAG)
    ledger = MockFillLedger(tmp_path/'fills.db', account_tag=TAG)
    for bad in (replace(snapshot(FILL), deposit=(('private-note', 'PRIVATE'),)),
                replace(snapshot(FILL), holdings=(('900001', 1, 2),)),
                snapshot(replace(FILL, quantity=True)),
                snapshot(FILL, replace(LATER, side='SELL')),
                snapshot(FILL, replace(FILL, price=990))):
        with pytest.raises(ContractError):
            ledger.ingest(bad)
    assert b'PRIVATE' not in ledger.path.read_bytes()


def test_full_fake_query_to_persistent_read_only_order_view(tmp_path):
    from test_kiwoom_mock_review import rig, ACCOUNT
    from hashlib import sha256
    client, http, reviewer = rig()
    tag = sha256(('MOCK:' + client.profile_id + ':' + ACCOUNT).encode()).hexdigest()
    path = tmp_path/'fills.db'
    ledger = MockFillLedger(path, account_tag=tag)
    reviewer.collect_into(ledger, expected_account=ACCOUNT, trade_date=DAY)
    restored = MockFillLedger(path, account_tag=tag)
    view = restored.project_order(DAY, '12', BUY)
    assert view.filled_qty == 3 and view.filled_value == 2980
    assert view.net_cash_flow is None  # Raw fee observations are not final costs.
    assert all('/ordr' not in url for url, _ in http.calls)
    assert ACCOUNT.encode() not in path.read_bytes()
