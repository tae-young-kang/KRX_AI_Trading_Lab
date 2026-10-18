"""Read-only controller/worker tests: fake connections, no QApplication/GUI."""
from copy import deepcopy
from datetime import timedelta, timezone

import pytest

from app.config import DEFAULTS
from app.kiwoom_mock_contract import ContractError, UncertainResponse
from app.kiwoom_mock_watch import MockWatchSession, require_mock_mode
from app.kiwoom_mock_stream import MockOrderStream
from app.runtime_lock import ReceiverLock
from test_kiwoom_mock_review import rig, wire, ACCOUNT
from test_kiwoom_mock_stream import FakeSocket, real
from test_kiwoom_mock_dispatch import Response
from test_kiwoom_mock_status import NOW


def setup(first=None):
    client,http,_ = rig(first=first,second=first)
    socket = FakeSocket()
    created = []
    def factory(t):
        created.append(1)
        return MockOrderStream(t,connector=lambda:socket)
    session = MockWatchSession(client,stream_factory=factory)
    return session,client,http,socket,created


def test_account_confirmation_and_socket_start_are_separate_user_actions():
    session,client,http,socket,created = setup()
    with pytest.raises(ContractError):
        session.start()
    report = session.verify(ACCOUNT)
    assert report['held'] == 3 and report['order_money'] == 97010
    assert report['ready_for_orders'] is False and ACCOUNT not in repr(report)
    assert created == [] and session.state == 'VERIFIED'
    http.responses.append(Response(dict(return_code=0,acctNo=ACCOUNT)))
    session.start()
    assert created == [1] and session.state == 'CONNECTING'
    socket.frames.extend([dict(trnm='LOGIN',return_code=0),dict(trnm='REG',return_code=0)])
    assert session.poll() == [] and session.poll() == []
    assert session.state == 'RECEIVING'
    client._clock = lambda:NOW
    socket.frames.append(real())
    notices = session.poll()
    assert len(notices) == 1 and '잔고를 다시 확인해야 해요' in notices[0]
    assert session.balance_stale and session.notice_count == 1
    assert session.ready_for_orders is False
    assert all('/ordr' not in url for url,_ in http.calls)
    session.close()
    assert socket.closed and http.closed and session._account is None and client._token is None


def test_unknown_existing_order_is_not_zero_reserved_money():
    first = wire()
    first[-1]['oso'] = [dict(ord_no='12',orig_ord_no='0',stk_cd='900001',ord_qty='3',oso_qty='2')]
    session,_,_,_,_ = setup(first)
    report = session.verify(ACCOUNT)
    assert report['pending'] == 2 and report['order_money'] is None
    session.close()


@pytest.mark.parametrize('change', ['expired','auth','profile','cancelled'])
def test_confirmation_must_be_rechecked_after_expiry_or_context_change(change):
    session,client,http,_,created = setup()
    session.verify(ACCOUNT)
    if change == 'expired':
        moment = client._clock()+timedelta(seconds=121)
        client._clock = lambda:moment
    elif change == 'auth':
        client._auth_generation = 'e'*32
    elif change == 'profile':
        client.profile_id = 'e'*32
    else:
        client.cancelled = lambda:True
    before = len(http.calls)
    with pytest.raises(UncertainResponse):
        session.start()
    assert created == [] and len(http.calls) == before
    assert session.state == 'CLOSED' and session._account is None


def test_stop_during_account_collection_sends_no_followup_requests():
    session,client,http,_,_ = setup()
    original = http.post
    stopped = [False]
    def post(url, **kwargs):
        response = original(url,**kwargs)
        stopped[0] = True
        return response
    http.post = post
    client.cancelled = lambda:stopped[0]
    before = len(http.calls)
    with pytest.raises(UncertainResponse):
        session.verify(ACCOUNT)
    assert len(http.calls) == before+1
    assert http.closed and client._token is None


def test_mode_gate_cannot_use_synthetic_or_real_quote_credentials():
    cfg = deepcopy(DEFAULTS)
    with pytest.raises(ContractError):
        require_mock_mode(cfg)
    cfg.update(data_mode='KIWOOM',kiwoom_env='REAL')
    with pytest.raises(ContractError):
        require_mock_mode(cfg)
    cfg['kiwoom_env'] = 'MOCK'
    require_mock_mode(cfg)


def test_worker_stop_after_verification_does_not_automatically_start_socket(tmp_path,monkeypatch):
    import app.kiwoom_mock_watch_worker as module
    session,client,http,_,created = setup()
    monkeypatch.setattr(client,'login',lambda *args:None)  # Already fake-authenticated.
    def factory(**kwargs):
        client.cancelled = kwargs['cancelled']
        return client
    monkeypatch.setattr(module,'MockTransport',factory)
    monkeypatch.setattr(module,'MockWatchSession',lambda transport:session)
    worker = module.MockWatchWorker(tmp_path,'fixture-key','fixture-secret',ACCOUNT)
    reports = []
    def verified(report):
        reports.append(report)
        worker.stop()
    worker.verified.connect(verified)
    worker.run()  # No event loop, QApplication, or actual background start.
    assert len(reports) == 1 and created == []
    assert worker._credentials == () and http.closed and client._token is None
    with ReceiverLock(tmp_path/'receiver.lock'):
        pass


def test_stop_before_thread_start_and_duplicate_window_do_not_log_in(tmp_path,monkeypatch):
    import app.kiwoom_mock_watch_worker as module
    called = []
    monkeypatch.setattr(module,'MockTransport',lambda **kwargs:called.append(1))
    worker = module.MockWatchWorker(tmp_path,'fixture-key','fixture-secret',ACCOUNT)
    worker.stop()
    worker.run()
    assert called == [] and worker._credentials == ()
    worker = module.MockWatchWorker(tmp_path,'fixture-key','fixture-secret',ACCOUNT)
    with ReceiverLock(tmp_path/'receiver.lock'):
        worker.run()
    assert called == [] and worker._credentials == ()


def test_connection_guard_uses_korean_calendar_even_for_utc_clock():
    session,client,http,_,_ = setup()
    session.verify(ACCOUNT)
    utc = client._clock().astimezone(timezone.utc)
    client._clock = lambda:utc
    http.responses.append(Response(dict(return_code=0,acctNo=ACCOUNT)))
    session.start()
    assert session.state == 'CONNECTING'
    session.close()
