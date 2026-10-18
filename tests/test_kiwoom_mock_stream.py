"""Injected sockets and fake HTTP only. Never connect to any broker."""
from datetime import timedelta
import json
import sys
import types

import pytest

from app.kiwoom_mock_stream import MockOrderStream, MOCK_SOCKET, _connect
from app.kiwoom_mock_contract import ContractError, UncertainResponse
from test_kiwoom_mock_dispatch import transport, Response
from test_kiwoom_mock_status import ACCOUNT, START, NOW, event


class FakeSocket:
    def __init__(self):
        self.frames = []
        self.sent = []
        self.closed = False

    def send(self, frame):
        self.sent.append(json.loads(frame))

    def recv(self, *, timeout):
        assert timeout == 1
        if not self.frames:
            raise TimeoutError
        frame = self.frames.pop(0)
        if isinstance(frame,Exception):
            raise frame
        return json.dumps(frame) if isinstance(frame,dict) else frame

    def close(self):
        self.closed = True


def rig(*, ready=True):
    client,http,_ = transport()
    clock, ticks = [START], [0.0]
    client._clock = lambda:clock[0]
    socket = FakeSocket()
    connects = []
    def connector():
        connects.append(1)
        return socket
    stream = MockOrderStream(client, connector=connector, monotonic=lambda:ticks[0])
    assert connects == [] and len(http.calls) == 1
    http.responses.append(Response(dict(return_code=0,acctNo=ACCOUNT)))
    stream.open(expected_account=ACCOUNT)
    if ready:
        socket.frames.extend([dict(trnm='LOGIN',return_code=0), dict(trnm='REG',return_code=0)])
        assert stream.read_once() == [] and stream.read_once() == []
        clock[0] = NOW
    return stream,client,http,socket,clock,ticks,connects


def real(*rows):
    return dict(trnm='REAL',data=list(rows) or [event()])


def test_account_login_registration_and_notices_never_send_orders():
    stream,client,http,socket,clock,_,connects = rig()
    assert [packet['trnm'] for packet in socket.sent] == ['LOGIN','REG']
    assert socket.sent[1] == dict(trnm='REG',grp_no='1',refresh='1',data=[dict(item=[''],type=['00'])])
    assert stream.state == 'RECEIVING' and '주문 불가' in stream.status
    socket.frames.append(real())
    notices = stream.read_once()
    assert len(notices) == 1 and stream.accepts(notices[0])
    assert notices[0].auth_generation == client.auth_generation
    assert notices[0].profile_id == client.profile_id
    assert ACCOUNT not in repr(notices[0]) and 'fixture-session' not in repr(stream)
    assert all('/ordr' not in url for url,_ in http.calls)
    assert connects == [1] and stream.ready_for_orders is False


def test_ping_echo_quiet_account_and_duplicate_frames():
    stream,_,_,socket,_,_,_ = rig()
    ping = dict(trnm='PING',data='fixture-heartbeat')
    socket.frames.append(ping)
    assert stream.read_once() == [] and socket.sent[-1] == ping
    assert stream.read_once() == [] and stream.state == 'RECEIVING'  # recv timeout only
    socket.frames.extend([real(event(),event()),real()])
    assert len(stream.read_once()) == 1
    assert stream.read_once() == []


@pytest.mark.parametrize('frame', [dict(trnm='LOGIN',return_code=1,return_msg='PRIVATE_RESPONSE'),
    dict(trnm='LOGIN',return_code=False),dict(trnm='LOGIN',return_code='0'),
    dict(trnm='REG',return_code=0),real()])
def test_handshake_must_be_confirmed_in_order(frame):
    stream,_,_,socket,_,_,connects = rig(ready=False)
    socket.frames.append(frame)
    with pytest.raises(UncertainResponse) as error:
        stream.read_once()
    assert 'PRIVATE_RESPONSE' not in str(error.value)
    assert stream.state == 'NEEDS_RECHECK' and stream.session is None and socket.closed
    with pytest.raises(ContractError):
        stream.open(expected_account=ACCOUNT)
    assert connects == [1]


@pytest.mark.parametrize('change', ['auth','profile','day','expired','socket'])
def test_connection_changes_invalidate_previous_notices_without_reconnecting(change):
    stream,client,_,socket,clock,_,connects = rig()
    socket.frames.append(real())
    notice = stream.read_once()[0]
    if change == 'auth':
        client._auth_generation = 'e'*32
    elif change == 'profile':
        client.profile_id = 'e'*32
    elif change == 'day':
        clock[0] += timedelta(days=1)
    elif change == 'expired':
        client._expires = clock[0]-timedelta(seconds=1)
    else:
        socket.frames.append(OSError('PRIVATE_RESPONSE'))
    with pytest.raises(UncertainResponse):
        stream.read_once()
    assert not stream.accepts(notice) and socket.closed
    assert stream.status == '연결 끊김 · 다시 확인 필요' and connects == [1]


def test_bad_batch_is_not_partially_delivered_and_foreign_accounts_stop_stream():
    stream,_,_,socket,_,_,_ = rig()
    socket.frames.append(real(event(), event(**{'9201':'9'*10})))
    with pytest.raises(UncertainResponse) as error:
        stream.read_once()
    assert '9'*10 not in str(error.value) and not stream._seen
    assert socket.closed


@pytest.mark.parametrize('frame', ['', 'not-json', '[]', 'x'*65537, dict(trnm='REAL',data=[]),
    real({'type':'0B','values':{}}),dict(trnm='SYSTEM')],
    ids=['empty','not-json','array','oversize','empty-rows','wrong-topic','system'])
def test_bad_or_unregistered_frames_fail_closed(frame):
    stream,_,_,socket,_,_,_ = rig()
    socket.frames.append(frame)
    with pytest.raises(UncertainResponse):
        stream.read_once()
    assert socket.closed


def test_unconfirmed_registration_timeout_and_close_wipe_session():
    stream,_,_,socket,_,ticks,_ = rig(ready=False)
    ticks[0] = 11
    with pytest.raises(UncertainResponse):
        stream.read_once()
    assert socket.closed
    stream,_,_,socket,_,_,_ = rig()
    socket.frames.append(real())
    notice = stream.read_once()[0]
    stream.close()
    stream.close()
    assert stream.session is None and stream._expected_account is None and not stream.accepts(notice)


def test_default_connector_has_fixed_mock_url_no_proxy_and_no_frame_logging(monkeypatch):
    captured = []
    fake_client = types.ModuleType('websockets.sync.client')
    def connect(url, **kwargs):
        captured.append((url,kwargs))
        return 'fixture-socket'
    fake_client.connect = connect
    monkeypatch.setitem(sys.modules,'websockets.sync.client',fake_client)
    assert _connect() == 'fixture-socket'
    url, options = captured[0]
    assert url == MOCK_SOCKET and 'mockapi.kiwoom.com' in url
    assert options['proxy'] is None and options['max_size'] == 65536
    assert options['logger'].propagate is False
    assert not options['logger'].isEnabledFor(50)


def test_account_mismatch_never_opens_socket():
    client,http,_ = transport()
    called = []
    stream = MockOrderStream(client,connector=lambda:called.append(1))
    http.responses.append(Response(dict(return_code=0,acctNo='9'*10)))
    with pytest.raises(UncertainResponse):
        stream.open(expected_account=ACCOUNT)
    assert called == [] and stream.session is None


@pytest.mark.parametrize('disconnect', [False, True])
def test_stream_notice_to_fresh_account_review_and_audit(tmp_path, disconnect):
    from test_kiwoom_mock_cancel_review import rig as review_rig
    from app.order_engine import connect
    parts = review_rig(tmp_path)
    client,http,journal,ledger,reviewer,_,clock,request = parts
    http.responses.insert(0,Response(dict(return_code=0,acctNo=ACCOUNT)))
    socket = FakeSocket()
    stream = MockOrderStream(client,connector=lambda:socket)
    stream.open(expected_account=ACCOUNT)
    socket.frames.extend([dict(trnm='LOGIN',return_code=0),dict(trnm='REG',return_code=0),
        real(event(**{'908':clock[0].strftime('%H%M%S')}))])
    stream.read_once()
    stream.read_once()
    notice = stream.read_once()[0]
    clock[0] += timedelta(seconds=1)
    if disconnect:
        original = reviewer.capture
        def closed(**kwargs):
            captured = original(**kwargs)
            socket.closed = True
            return captured
        reviewer.capture = closed
        with pytest.raises(UncertainResponse):
            stream.review_cancellation(journal,ledger,reviewer,'buy',notice,known_orders={'0000012':request})
        with connect(ledger.path) as db:
            assert db.execute('SELECT count(*) FROM cancel_review_events').fetchone()[0] == 0
        assert stream.state == 'NEEDS_RECHECK'
    else:
        result = stream.review_cancellation(journal,ledger,reviewer,'buy',notice,known_orders={'0000012':request})
        assert result.remaining_quantity == 0 and not result.ready_for_orders
        with connect(ledger.path) as db:
            assert db.execute('SELECT count(*) FROM cancel_review_events').fetchone()[0] == 1
