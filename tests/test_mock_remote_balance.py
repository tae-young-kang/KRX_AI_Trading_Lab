"""Offline remote mailbox and fresh account capture; no GUI or real IO."""
import pytest
from app.mock_watch_status import MockWatchStatus
from app.kiwoom_mock_contract import UncertainResponse
from test_kiwoom_mock_watch import setup
from test_kiwoom_mock_review import ACCOUNT, wire
from test_kiwoom_mock_dispatch import Response
from test_telegram_orders import sandbox, update


def test_remote_request_auth_replay_stop_and_mode(sandbox):
    service, broker, _, _ = sandbox
    view = MockWatchStatus()
    service.watch_refresh, service.watch_result = view.request_refresh, view.refresh_result
    service.orders_enabled = False
    assert '연결이 필요' in service.handle(update(sandbox,1,'/잔고'))
    view.enable_refresh()
    bad = update(sandbox,2,'/잔고')
    bad['message']['from']['id'] = 99
    assert service.handle(bad) is None and not view.take_refresh()
    message = update(sandbox,3,'/잔고')
    reply = service.handle(message)
    assert '확인 중' in reply and view.take_refresh()
    assert service.handle(message) == reply and not view.take_refresh()
    assert '이미' in service.handle(update(sandbox,4,'/잔고'))
    report = dict(checked_at='10/19 10:00:00',order_money=100,held=3,pending=0,positions=(('900001',3),))
    view.finish_refresh(report)
    result = service.handle(update(sandbox,5,'/잔고결과'))
    assert '조회 완료' in result and '900001 3주' in result
    assert '30초' in service.handle(update(sandbox,6,'/잔고'))
    service.handle(update(sandbox,7,'/중지'))
    assert '중지' in service.handle(update(sandbox,8,'/잔고'))
    service.orders_enabled = True
    assert '결과가 없' in service.handle(update(sandbox,9,'/잔고결과'))
    assert broker.calls == []


def test_pending_failure_and_restart_do_not_keep_work():
    view = MockWatchStatus()
    view.enable_refresh()
    view.request_refresh()
    assert view.take_refresh()
    view.stopped()
    assert '완료하지 못' in view.refresh_result()
    assert not view.take_refresh() and '연결이 필요' in view.request_refresh()
    view.reset()
    assert '아직 새 잔고' in view.refresh_result()


def test_worker_consumes_request_on_owner_loop_and_cleans_up(tmp_path,monkeypatch):
    import app.kiwoom_mock_watch_worker as module
    session,client,http,socket,_ = setup()
    monkeypatch.setattr(client,'login',lambda *args:None)
    def factory(**kwargs):
        client.cancelled = kwargs['cancelled']
        return client
    monkeypatch.setattr(module,'MockTransport',factory)
    monkeypatch.setattr(module,'MockWatchSession',lambda transport:session)
    http.responses.append(Response(dict(return_code=0,acctNo=ACCOUNT)))
    identity = dict(return_code=0,acctNo=ACCOUNT)
    http.responses.extend(Response(x,headers={'cont-yn':'N'}) for x in [identity]+wire()+wire()+[identity])
    socket.frames.extend([dict(trnm='LOGIN',return_code=0),dict(trnm='REG',return_code=0)])
    view = MockWatchStatus()
    worker = module.MockWatchWorker(tmp_path,'fixture-key','fixture-secret',ACCOUNT,view)
    reports = []
    def verified(report):
        reports.append(report)
        if len(reports)==1: worker.start_listening()
        else: worker.stop()
    worker.verified.connect(verified)
    original = view.enable_refresh
    def enable():
        original()
        view.request_refresh()
    monkeypatch.setattr(view,'enable_refresh',enable)
    worker.run()
    assert len(reports)==2 and reports[-1]['positions'] == (('900001',3),)
    assert http.closed and socket.closed and worker._credentials == ()
    assert not view.take_refresh() and '최신 값이 아닐 수' in view.refresh_result()


@pytest.mark.parametrize('fail', [False,True])
def test_fresh_capture_same_session_read_only_and_failure_closes(fail):
    session,client,http,socket,_ = setup()
    session.verify(ACCOUNT)
    http.responses.append(Response(dict(return_code=0,acctNo=ACCOUNT)))
    session.start()
    socket.frames.extend([dict(trnm='LOGIN',return_code=0),dict(trnm='REG',return_code=0)])
    session.poll(); session.poll()
    identity = dict(return_code=0,acctNo=ACCOUNT)
    frames = [identity]+wire()+wire()+[identity]
    if fail:
        frames[-1] = dict(return_code=0,acctNo='9'*10)
    http.responses.extend(Response(x,headers={'cont-yn':'N'}) for x in frames)
    before = len(http.calls)
    if fail:
        with pytest.raises(UncertainResponse): session.refresh()
        assert session.state == 'CLOSED' and session.balance_stale
    else:
        report = session.refresh()
        assert report['held'] == 3 and report['positions'] == (('900001',3),)
        assert ACCOUNT not in repr(report) and report['ready_for_orders'] is False
        assert session.state == 'RECEIVING'
    assert len(http.calls) > before
    assert all(options['headers'].get('api-id') not in ('kt10000','kt10001','kt10002','kt10003')
               for _,options in http.calls)
    session.close()
