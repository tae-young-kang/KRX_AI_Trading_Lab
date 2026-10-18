"""Only temporary databases and fake HTTP. Never contact any server."""
from datetime import datetime
import json

import pytest

from app.kiwoom_mock_contract import (
    MockPlan, ContractError, UncertainResponse, order_plan, amend_plan, cancel_plan,
)
from app.kiwoom_mock_transport import MockTransport
from app.kiwoom_mock_journal import MockDispatchJournal
from app.order_engine import OrderRequest, OrderEngine, connect
from app.runtime_lock import ReceiverLock, ReceiverAlreadyRunning
from app.trading_clock import KST, TradingCalendar

PROFILE = 'a' * 32  # Opaque synthetic local profile, not an account ID.
NOW = datetime(2026, 10, 19, 10, tzinfo=KST)
BUY = order_plan(OrderRequest('900001', 'BUY', 2, 1000))


class Response:
    def __init__(self, payload, status=200, headers=None):
        self.payload, self.status_code = payload, status
        self.headers = headers or {}

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeHTTP:
    def __init__(self):
        self.responses = []
        self.calls = []
        self.closed = False

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def close(self):
        self.closed = True


def transport(profile=PROFILE):
    http = FakeHTTP()
    pauses = []
    client = MockTransport(environment='MOCK', profile_id=profile, session=http,
        clock=lambda: NOW, monotonic=lambda: 1, pause=pauses.append)
    http.responses.append(Response(dict(return_code=0, token='fixture-session',
        token_type='bearer', expires_dt='20261019110000')))
    client.login('fixture-key', 'fixture-secret')
    return client, http, pauses


def accepted(order='0000012', base=None):
    body = dict(return_code=0, ord_no=order)
    if base:
        body['base_orig_ord_no'] = base
    return Response(body)


def test_auth_mock_only_expiry_redaction_and_close():
    with pytest.raises(ContractError):
        MockTransport(environment='LIVE', profile_id=PROFILE)
    client, http, pauses = transport()
    http.responses.append(accepted())
    client.exchange(BUY)
    assert all(url.startswith('https://mockapi.kiwoom.com/') for url, _ in http.calls)
    assert all(options['allow_redirects'] is False for _, options in http.calls)
    assert pauses == [1.1]
    client._clock = lambda: datetime(2026, 10, 19, 12, tzinfo=KST)
    with pytest.raises(ContractError):
        client.exchange(BUY)
    assert len(http.calls) == 2
    client.close()
    assert client._token is None and http.closed
    assert 'fixture-session' not in repr(client)


@pytest.mark.parametrize('response', [Response({}, 302), Response({}, 429), Response({}, 500),
    Response(ValueError('PRIVATE_RESPONSE')), TimeoutError('PRIVATE_RESPONSE')])
def test_single_attempt_failure_is_redacted(response):
    client, http, _ = transport()
    http.responses.append(response)
    with pytest.raises(UncertainResponse) as error:
        client.exchange(BUY)
    assert len(http.calls) == 2
    assert 'PRIVATE_RESPONSE' not in str(error.value)
    assert error.value.__cause__ is None


def test_relogin_failure_discards_old_token():
    client, http, _ = transport()
    http.responses.append(Response(dict(return_code=0, token='bad token',
        token_type='bearer', expires_dt='20261019110000')))
    with pytest.raises(UncertainResponse):
        client.login('fixture-key', 'fixture-secret')
    with pytest.raises(ContractError):
        client.ready()


def test_handcrafted_plan_and_mutation_cursor_never_reach_http():
    client, http, _ = transport()
    for plan in [MockPlan('not-an-api', ()), MockPlan(BUY.api_id, BUY.fields + (('account', 'x'),)),
                 MockPlan(BUY.api_id, tuple((k, '3' if k == 'trde_tp' else v) for k, v in BUY.fields))]:
        with pytest.raises(ContractError):
            client.exchange(plan)
    with pytest.raises(ContractError):
        client.exchange(BUY, cursor='some-page')
    assert len(http.calls) == 1


def test_durable_duplicate_and_changed_payload(tmp_path):
    client, http, _ = transport()
    path = tmp_path/'mock.db'
    journal = MockDispatchJournal(path, client)
    http.responses.append(accepted())
    first = journal.dispatch('request-1', BUY)
    assert first['state'] == 'ACKNOWLEDGED'
    assert journal.dispatch('request-1', BUY) == first
    reopened = MockDispatchJournal(path, client)
    assert reopened.dispatch('request-1', BUY) == first
    assert len(http.calls) == 2
    with pytest.raises(ContractError):
        journal.dispatch('request-1', order_plan(OrderRequest('900001', 'BUY', 3, 1000)))
    raw = path.read_bytes()
    assert b'fixture-session' not in raw and b'fixture-secret' not in raw
    # MOCK adapter never bypasses the existing synthetic-engine gate.
    with pytest.raises(ValueError):
        OrderEngine(tmp_path/'engine.db', client, TradingCalendar())


def test_unknown_blocks_restart_and_new_keys_without_resend(tmp_path):
    client, http, _ = transport()
    path = tmp_path/'mock.db'
    journal = MockDispatchJournal(path, client)
    http.responses.append(TimeoutError('lost acknowledgement'))
    assert journal.dispatch('uncertain', BUY)['state'] == 'UNKNOWN'
    journal = MockDispatchJournal(path, client)
    assert journal.dispatch('uncertain', BUY)['state'] == 'UNKNOWN'
    with pytest.raises(ContractError):
        journal.dispatch('new-key', BUY)
    assert len(http.calls) == 2


def test_process_exit_after_acceptance_never_replays(tmp_path):
    client, http, _ = transport()
    path = tmp_path/'mock.db'
    journal = MockDispatchJournal(path, client)
    http.responses.append(SystemExit())
    with pytest.raises(SystemExit):
        journal.dispatch('crash', BUY)
    assert journal.get('crash')['state'] == 'SENDING'
    recovered = MockDispatchJournal(path, client)
    assert recovered.dispatch('crash', BUY)['state'] == 'UNKNOWN'
    assert len(http.calls) == 2


def test_definite_refusal_not_retried_and_can_use_new_key(tmp_path):
    client, http, _ = transport()
    journal = MockDispatchJournal(tmp_path/'mock.db', client)
    http.responses.append(Response(dict(return_code=10, return_msg='PRIVATE_RESPONSE')))
    assert journal.dispatch('refused', BUY)['state'] == 'REJECTED'
    assert journal.dispatch('refused', BUY)['state'] == 'REJECTED'
    http.responses.append(accepted())
    assert journal.dispatch('new-request', BUY)['state'] == 'ACKNOWLEDGED'
    assert 'PRIVATE_RESPONSE' not in json.dumps(journal.get('refused'))


def test_amend_cancel_chain_and_stale_parent(tmp_path):
    client, http, _ = transport()
    journal = MockDispatchJournal(tmp_path/'mock.db', client)
    http.responses.extend([accepted(), accepted('0000013', '0000012'), accepted('0000014', '0000012')])
    journal.dispatch('buy', BUY)
    amend = amend_plan('900001', '0000012', remaining_quantity=1, limit_price=1100)
    modified = journal.dispatch('modify', amend, parent_key='buy')
    assert modified['root_key'] == 'buy' and modified['order_no'] == '0000013'
    with pytest.raises(ContractError):
        journal.dispatch('stale', cancel_plan('900001', '0000012'), parent_key='buy')
    canceled = journal.dispatch('cancel', cancel_plan('900001', '0000013'), parent_key='modify')
    assert canceled['state'] == 'ACKNOWLEDGED'  # NOT CANCELED/FILLED.
    with pytest.raises(ContractError):
        journal.dispatch('cancel-again', cancel_plan('900001', '0000014'), parent_key='cancel')
    assert len(http.calls) == 4


@pytest.mark.parametrize('ack', [accepted('0000013', '0000099'), accepted('0000012', '0000012')])
def test_bad_lineage_or_reused_order_number_blocks(tmp_path, ack):
    client, http, _ = transport()
    journal = MockDispatchJournal(tmp_path/'mock.db', client)
    http.responses.extend([accepted(), ack])
    journal.dispatch('buy', BUY)
    assert journal.dispatch('cancel', cancel_plan('900001', '0000012'), parent_key='buy')['state'] == 'UNKNOWN'
    with pytest.raises(ContractError):
        journal.dispatch('new', BUY)


def test_profile_and_lock_prevent_mixed_or_parallel_dispatch(tmp_path):
    client, http, _ = transport()
    path = tmp_path/'mock.db'
    journal = MockDispatchJournal(path, client)
    other, _, _ = transport('b' * 32)
    with pytest.raises(ContractError):
        MockDispatchJournal(path, other)
    with ReceiverLock(journal._lock_path):
        with pytest.raises(ReceiverAlreadyRunning):
            journal.dispatch('blocked', BUY)
        with pytest.raises(ReceiverAlreadyRunning):
            MockDispatchJournal(path, client)
    assert journal.get('blocked') is None and len(http.calls) == 1


def test_pages_complete_or_fail_without_partial_account():
    client, http, _ = transport()
    row = dict(stk_cd='A900001', rmnd_qty='2', trde_able_qty='1', acnt_no='PRIVATE_ACCOUNT')
    http.responses.extend([Response(dict(return_code=0, acnt_evlt_remn_indv_tot=[row]),
                                   headers={'cont-yn': 'Y', 'next-key': 'page-two'}),
                           Response(dict(return_code=0, acnt_evlt_remn_indv_tot=[]),
                                   headers={'cont-yn': 'N'})])
    assert client.read_all('holdings') == [dict(stk_cd='A900001', rmnd_qty='2', trde_able_qty='1')]
    assert http.calls[-1][1]['headers']['next-key'] == 'page-two'
    repeat = Response(dict(return_code=0, oso=[]), headers={'cont-yn': 'Y', 'next-key': 'repeat'})
    http.responses.extend([repeat, repeat])
    with pytest.raises(UncertainResponse):
        client.read_all('unfilled')
    http.responses.append(repeat)
    with pytest.raises(UncertainResponse):
        client.read_all('unfilled', max_pages=1)


def test_unrelated_database_and_missing_auth_do_not_dispatch(tmp_path):
    client, http, _ = transport()
    path = tmp_path/'practice.db'
    with connect(path) as db:
        db.execute('CREATE TABLE practice (value INTEGER)')
    with pytest.raises(ContractError):
        MockDispatchJournal(path, client)
    journal = MockDispatchJournal(tmp_path/'mock.db', client)
    client.close()
    with pytest.raises(ContractError):
        journal.dispatch('not-sent', BUY)
    assert journal.get('not-sent') is None and len(http.calls) == 1


@pytest.mark.parametrize('response', [
    Response(dict(return_code=10, ord_no='0000012')),
    Response(dict(return_code=0, ord_no='0000012'), headers={'Api-Id': 'kt10001'}),
])
def test_contradictory_response_is_unknown(tmp_path, response):
    client, http, _ = transport()
    journal = MockDispatchJournal(tmp_path/'mock.db', client)
    http.responses.append(response)
    assert journal.dispatch('uncertain', BUY)['state'] == 'UNKNOWN'
