"""Fake HTTP and temporary databases only; no trading program is launched."""
from dataclasses import replace
from datetime import datetime
import json

import pytest

from app.kiwoom_mock_chain import project_chain
from app.kiwoom_mock_contract import ContractError, amend_plan, cancel_plan
from app.kiwoom_mock_journal import MockDispatchJournal
from app.kiwoom_mock_ledger import MockFillLedger
from app.order_engine import connect
from app.trading_clock import KST
from test_kiwoom_mock_dispatch import transport, accepted, BUY
from test_kiwoom_mock_ledger import DAY, TAG, FILL, LATER, snapshot, confirm


def rig(tmp_path, *, cancel=False):
    client, http, _ = transport()
    journal = MockDispatchJournal(tmp_path/'dispatch.db', client)
    journal.bind_account(TAG)
    http.responses.extend([accepted(), accepted('0000013', '0000012')])
    journal.dispatch('buy', BUY)
    journal.dispatch('modify', amend_plan('900001', '12', remaining_quantity=2, limit_price=990), parent_key='buy')
    if cancel:
        http.responses.append(accepted('0000014', '0000012'))
        journal.dispatch('cancel', cancel_plan('900001', '13'), parent_key='modify')
    ledger = MockFillLedger(tmp_path/'fills.db', account_tag=TAG)
    child = replace(LATER, order_no='0000013', original_no='0000012')
    return client, http, journal, ledger, child


def test_chain_totals_costs_restart_and_cancel_ack_is_not_completion(tmp_path):
    client, http, journal, ledger, child = rig(tmp_path, cancel=True)
    ledger.ingest(snapshot(FILL, child))
    confirm(ledger)
    view = project_chain(journal, ledger, 'buy')
    assert view.order_nos == ('0000012', '0000013', '0000014')
    assert view.filled_qty == 3 and view.filled_value == 2980
    assert view.commission is None and view.net_cash_flow is None
    assert view.cancel_request_acknowledged and not view.ready_for_orders
    assert view.state == 'CHAIN_OBSERVATIONS_ONLY'
    confirm(ledger, order='13', quantity=2, gross=1980, commission=20, item='line-2')
    journal = MockDispatchJournal(journal.path, client)
    ledger = MockFillLedger(ledger.path, account_tag=TAG)
    view = project_chain(journal, ledger, 'buy')
    assert view.commission == 30 and view.net_cash_flow == -3010
    before = len(http.calls)
    assert project_chain(journal, ledger, 'buy') == view
    assert len(http.calls) == before


def test_day_scoped_reused_order_number_and_cross_day_parent_block(tmp_path):
    client, http, _ = transport()
    journal = MockDispatchJournal(tmp_path/'dispatch.db', client)
    http.responses.append(accepted())
    first = journal.dispatch('day-one', BUY)
    client._clock = lambda: datetime(2026, 10, 20, 10, tzinfo=KST)
    # Explicit fake reauthentication; test fixture extends expiry without HTTP.
    client._expires = datetime(2026, 10, 20, 11, tzinfo=KST)
    http.responses.append(accepted())
    second = journal.dispatch('day-two', BUY)
    assert first['order_no'] == second['order_no']
    assert first['trade_day'] != second['trade_day']
    assert second['state'] == 'ACKNOWLEDGED'
    with pytest.raises(ContractError):
        journal.dispatch('wrong-parent', cancel_plan('900001', '12'), parent_key='day-one')
    assert journal.get('wrong-parent') is None


def test_undated_legacy_receipts_preserved_but_never_replayed(tmp_path):
    client, http, _ = transport()
    path = tmp_path/'legacy.db'
    with connect(path) as db:
        db.execute('''CREATE TABLE mock_dispatch (request_key TEXT PRIMARY KEY, api_id TEXT,
            payload TEXT,parent_key TEXT,root_key TEXT,state TEXT,order_no TEXT UNIQUE,base_order_no TEXT)''')
        db.execute('INSERT INTO mock_dispatch VALUES (?,?,?,?,?,?,?,?)',
                   ('old', BUY.api_id, json.dumps(BUY.body(), sort_keys=True, separators=(',', ':')),
                    None, 'old', 'ACKNOWLEDGED', '0000012', None))
    journal = MockDispatchJournal(path, client)
    old = journal.get('old')
    assert old['trade_day'] is None and old['order_no'] == '0000012'
    assert journal.dispatch('old', BUY) == old
    with pytest.raises(ContractError):
        journal.dispatch('new', BUY)
    with pytest.raises(ContractError):
        journal.bind_account(TAG)
    assert len(http.calls) == 1


@pytest.mark.parametrize('mutation', ['unknown', 'different-day', 'bad-parent', 'cycle'])
def test_uncertain_or_broken_chain_cannot_produce_aggregate(tmp_path, mutation):
    _, _, journal, ledger, child = rig(tmp_path)
    ledger.ingest(snapshot(FILL, child))
    with connect(journal.path) as db:
        if mutation == 'unknown':
            db.execute("UPDATE mock_dispatch SET state='UNKNOWN' WHERE request_key='modify'")
        elif mutation == 'different-day':
            db.execute("UPDATE mock_dispatch SET trade_day='2026-10-20' WHERE request_key='modify'")
        elif mutation == 'bad-parent':
            db.execute("UPDATE mock_dispatch SET parent_key='missing' WHERE request_key='modify'")
        else:
            db.execute("UPDATE mock_dispatch SET parent_key='modify' WHERE request_key='modify'")
    with pytest.raises(ContractError):
        project_chain(journal, ledger, 'buy')


@pytest.mark.parametrize('changes', [dict(fill_no='0000001'), dict(price=991),
    dict(original_no='0000099'), dict(side='SELL'), dict(order_no='0000099')])
def test_conflicting_or_unmapped_fills_are_not_silently_counted(tmp_path, changes):
    _, _, journal, ledger, child = rig(tmp_path)
    ledger.ingest(snapshot(FILL, replace(child, **changes)))
    with pytest.raises(ContractError):
        project_chain(journal, ledger, 'buy')


def test_unbound_or_different_account_cannot_join(tmp_path):
    client, _, journal, ledger, _ = rig(tmp_path)
    other = MockFillLedger(tmp_path/'other.db', account_tag='d' * 64)
    with pytest.raises(ContractError):
        project_chain(journal, other, 'buy')
    with pytest.raises(ContractError):
        journal.bind_account('d' * 64)
    unbound = MockDispatchJournal(tmp_path/'unbound.db', client)
    with pytest.raises(ContractError):
        project_chain(unbound, ledger, 'buy')


@pytest.mark.parametrize('change', ['date', 'auth', 'profile'])
def test_context_change_during_send_is_unknown_and_not_retried(tmp_path, change):
    client, http, _ = transport()
    journal = MockDispatchJournal(tmp_path/'dispatch.db', client)
    old_post = http.post
    http.responses.append(accepted())

    def rollover(url, **kwargs):
        response = old_post(url, **kwargs)
        if change == 'date':
            client._clock = lambda: datetime(2026, 10, 20, 0, tzinfo=KST)
        elif change == 'auth':
            client._auth_generation = 'different-session'
        else:
            client.profile_id = 'e' * 32
        return response

    http.post = rollover
    assert journal.dispatch('midnight', BUY)['state'] == 'UNKNOWN'
    if change == 'profile':
        with pytest.raises(ContractError):
            journal.dispatch('midnight', BUY)
        client.profile_id = journal._profile
    assert journal.dispatch('midnight', BUY)['state'] == 'UNKNOWN'
    assert len(http.calls) == 2


def test_zero_means_remaining_not_unlimited_quantity(tmp_path):
    _, _, journal, ledger, child = rig(tmp_path)
    with connect(journal.path) as db:
        row = db.execute("SELECT payload FROM mock_dispatch WHERE request_key='modify'").fetchone()
        body = json.loads(row[0])
        body['mdfy_qty'] = '0'
        db.execute("UPDATE mock_dispatch SET payload=? WHERE request_key='modify'", (json.dumps(body),))
    ledger.ingest(snapshot(FILL, child))
    with pytest.raises(ContractError):
        project_chain(journal, ledger, 'buy')
