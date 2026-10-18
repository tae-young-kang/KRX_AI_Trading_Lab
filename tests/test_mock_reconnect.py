"""Explicit remote recovery with fake IO only; no GUI event loop."""
from datetime import timedelta
from types import SimpleNamespace
import pytest
from app.mock_watch_status import MockWatchStatus
from app.runtime_lock import ReceiverLock, ReceiverAlreadyRunning
from test_kiwoom_mock_status import NOW
from test_telegram_orders import sandbox, update


def test_reconnect_authorization_mode_stop_and_update_replay(sandbox):
    service,broker,_,_ = sandbox
    view = MockWatchStatus()
    service.watch_reconnect = view.request_reconnect
    service.orders_enabled = False
    assert '지금은' in service.handle(update(sandbox,1,'/모의재연결'))
    view.offer_reconnect()
    bad = update(sandbox,2,'/모의재연결')
    bad['message']['chat']['type'] = 'group'
    assert service.handle(bad) is None and not view.take_reconnect()
    message = update(sandbox,3,'/모의재연결')
    reply = service.handle(message)
    assert '요청 대기' in reply and view.take_reconnect()
    assert service.handle(message) == reply and not view.take_reconnect()
    view.offer_reconnect()
    assert '30초' in service.handle(update(sandbox,4,'/모의재연결'))
    service.handle(update(sandbox,5,'/중지'))
    assert '중지' in service.handle(update(sandbox,6,'/모의재연결'))
    service.orders_enabled = True
    assert '허용되지' in service.handle(update(sandbox,7,'/모의재연결'))
    assert broker.calls == []


def test_reconnect_mailbox_drops_old_refresh_and_stop_clears_pending():
    view = MockWatchStatus()
    view.enable_refresh()
    view.request_refresh()
    view.offer_reconnect()
    assert not view.take_refresh() and '완료하지 못' in view.refresh_result()
    view.request_reconnect()
    assert '이미' in view.request_reconnect()
    view.stopped()
    assert not view.take_reconnect()
    assert '지금은' in view.request_reconnect()


@pytest.mark.parametrize('shift',[timedelta(days=1),timedelta(seconds=-1)])
def test_recovery_permission_expires_and_discards_memory(tmp_path,shift):
    from app.kiwoom_mock_watch_worker import MockWatchWorker
    clock = [NOW]
    worker = MockWatchWorker(tmp_path,'fixture-key','fixture-secret','0'*10,
                             allow_recovery=True,clock=lambda:clock[0])
    worker._ever_connected = True
    assert worker._recovery_valid()
    clock[0] += shift
    assert not worker._recovery_valid() and worker._recovery_credentials == ()
    worker.stop()
    worker.run()
    assert worker._credentials == ()


@pytest.mark.parametrize('scenario',['recover','default','stop','expiry','initial_failure','handshake_failure','cleanup_failure','failed_retry'])
def test_worker_recovery_is_explicit_scoped_and_cleans_credentials(tmp_path,monkeypatch,scenario):
    import app.kiwoom_mock_watch_worker as module
    moment = [NOW]
    events,transports = [],[]
    view = MockWatchStatus(clock=lambda:moment[0])
    worker = module.MockWatchWorker(tmp_path,'fixture-key','fixture-secret','0'*10,view,
                                   allow_recovery=scenario!='default',clock=lambda:moment[0])
    class Transport:
        def __init__(self,**kwargs):
            assert kwargs['environment']=='MOCK'
            self.number = len(transports)
            transports.append(self)
        def login(self,*args):
            with pytest.raises(ReceiverAlreadyRunning):
                with ReceiverLock(tmp_path/'receiver.lock'): pass
            events.append(('login',self.number))
            if scenario=='initial_failure' or (scenario=='failed_retry' and self.number==1):
                raise ValueError('fake failure')
        def close(self): events.append(('close',self.number))
    class Session:
        def __init__(self,transport):
            self.transport = transport
            self.state = 'CONNECTING'
            self.stream = SimpleNamespace(status='모의 알림 받는 중')
            self.count = 0
        def verify(self,account):
            events.append(('verify',self.transport.number))
            return dict(checked_at='10/19 10:00:00',order_money=1,held=0,pending=0)
        def start(self): events.append(('start',self.transport.number))
        def poll(self):
            self.count += 1
            if scenario=='handshake_failure': raise ValueError('fake handshake')
            if self.transport.number==1:
                worker.stop()
                return []
            if self.count==1:
                self.state='RECEIVING'
                return []
            raise ValueError('fake disconnect')
        def close(self):
            self.transport.close()
            if scenario=='cleanup_failure': raise ValueError('fake cleanup')
    monkeypatch.setattr(module,'MockTransport',Transport)
    monkeypatch.setattr(module,'MockWatchSession',Session)
    worker.verified.connect(lambda report:worker.start_listening())
    offered = []
    def status(text):
        if '/모의재연결' not in text: return
        offered.append(text)
        # No new connection occurred before an explicit remote request.
        assert len(transports)==1 if len(offered)==1 else len(transports)==2
        assert events[-1][0]=='close'
        if scenario=='expiry': moment[0] += timedelta(days=1)
        elif scenario=='stop': worker.stop()
        elif scenario=='failed_retry' and len(offered)==2:
            assert '30초' in view.request_reconnect()
            worker.stop()
        else: assert '요청 대기' in view.request_reconnect()
    worker.status.connect(status)
    worker.run()
    assert len(transports)==(2 if scenario in ('recover','failed_retry') else 1)
    assert bool(offered)==(scenario in ('recover','stop','expiry','failed_retry'))
    if scenario=='recover':
        assert events.index(('close',0)) < events.index(('login',1)) < events.index(('verify',1)) < events.index(('start',1))
    assert worker._credentials == worker._recovery_credentials == ()
    assert not view.take_reconnect() and not view.take_refresh()
    assert 'fixture' not in view.summary() and '0'*10 not in view.summary()
    with ReceiverLock(tmp_path/'receiver.lock'): pass
