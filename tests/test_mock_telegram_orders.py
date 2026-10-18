"""All broker requests are captured by FakeHTTP; all stores are temporary."""
from datetime import timedelta
import re
from concurrent.futures import ThreadPoolExecutor
import pytest

from app.mock_telegram_orders import MockTelegramOrders
from app.order_engine import connect
from test_kiwoom_mock_watch import setup
from test_kiwoom_mock_review import ACCOUNT, wire
from test_kiwoom_mock_dispatch import Response, accepted, NOW
from test_telegram_orders import sandbox, update


def rig(tmp_path, first=None):
    clock=[NOW]
    commands=MockTelegramOrders(tmp_path/'mock',confirmed_day=NOW.date(),clock=lambda:clock[0])
    session,client,http,socket,_=setup(first)
    client.profile_id=commands.profile
    client._clock=lambda:clock[0]
    session.verify(ACCOUNT)
    http.responses.append(Response(dict(return_code=0,acctNo=ACCOUNT)))
    session.start()
    socket.frames.extend([dict(trnm='LOGIN',return_code=0),dict(trnm='REG',return_code=0)])
    session.poll(); session.poll()
    commands.arm(session)
    return commands,session,http,socket,clock


def handle(commands,text,uid=1,scope='owner',session='receiver'):
    return commands.handle(text,scope=scope,session=session,uid=uid,now=commands.clock().timestamp())


def queue(commands,text='/매수 900001 3 1000',uid=1):
    preview=handle(commands,text,uid=uid)
    token=re.search('/모의확인 ([A-Za-z0-9_-]+)',preview)[1]
    assert '실제 돈 사용 안 함' in preview
    assert '전송을 요청' in handle(commands,'/모의확인 '+token,uid=uid+1)
    return token


def add_capture(http,first=None):
    identity=dict(return_code=0,acctNo=ACCOUNT)
    frames=[identity]+(first or wire())+(first or wire())+[identity]
    http.responses.extend(Response(x,headers={'cont-yn':'N'}) for x in frames)


def mutations(http):
    return [options for _,options in http.calls if options['headers'].get('api-id') in ('kt10000','kt10001','kt10002','kt10003')]


def test_confirm_is_queued_then_owner_dispatches_once_and_receipt_is_not_fill(tmp_path):
    cmd,session,http,_,_=rig(tmp_path)
    token=queue(cmd)
    assert mutations(http)==[]
    assert '유효한' in handle(cmd,'/모의확인 '+token,uid=3)
    add_capture(http)
    http.responses.append(accepted('0000013'))
    assert cmd.process(session)=='ACKNOWLEDGED'
    assert cmd.process(session) is None and len(mutations(http))==1
    assert mutations(http)[0]['headers']['api-id']=='kt10000'
    assert '접수 응답 확인 · 체결은 별도 조회' in handle(cmd,'/주문상태')
    assert '체결 수량 조회 일치' not in handle(cmd,'/주문상태')
    assert session.balance_stale
    cmd.disarm(); session.close()


@pytest.mark.parametrize('response',[TimeoutError('PRIVATE'),Response({},status=500)])
def test_unknown_blocks_restart_and_never_retries(tmp_path,response):
    cmd,session,http,_,_=rig(tmp_path)
    queue(cmd)
    add_capture(http);http.responses.append(response)
    assert cmd.process(session)=='UNKNOWN'
    assert '재주문 금지' in handle(cmd,'/주문상태')
    cmd.disarm()
    replacement=MockTelegramOrders(tmp_path/'mock',confirmed_day=NOW.date(),clock=lambda:NOW)
    replacement.arm(session)
    assert '미확인 주문' in handle(replacement,'/매수 900001 1 1000')
    assert replacement.process(session) is None and len(mutations(http))==1
    assert 'PRIVATE' not in handle(replacement,'/주문상태')
    replacement.disarm();session.close()


@pytest.mark.parametrize('case',['money','sellable','pending','expired','auth','stopped','disconnect'])
def test_preflight_blocks_without_any_mutation(tmp_path,case):
    cmd,session,http,socket,clock=rig(tmp_path)
    queue(cmd,'/매도 900001 3 1000' if case=='sellable' else '/매수 900001 3 1000')
    data=wire()
    if case=='money': data[0]['ord_alow_amt']='100'
    if case=='pending': data[-1]['oso']=[dict(ord_no='12',orig_ord_no='0',stk_cd='900001',ord_qty='3',oso_qty='1')]
    if case=='expired': clock[0]+=timedelta(seconds=121)
    if case=='auth': session.transport._auth_generation='e'*32
    if case=='stopped': cmd.cancel_session('owner','receiver')
    if case=='disconnect': socket.frames.append(ConnectionError('PRIVATE'))
    add_capture(http,data)
    result=cmd.process(session)
    assert result in ('FAILED',None) and mutations(http)==[]
    cmd.disarm();session.close()


def test_cancel_arriving_during_account_capture_prevents_dispatch(tmp_path,monkeypatch):
    cmd,session,http,_,_=rig(tmp_path)
    queue(cmd);add_capture(http)
    original=session.capture_for_order
    def capture():
        observed=original()
        cmd.cancel_session('owner','receiver')
        return observed
    monkeypatch.setattr(session,'capture_for_order',capture)
    assert cmd.process(session)=='FAILED' and mutations(http)==[]
    cmd.disarm();session.close()


def test_daily_count_is_durable_and_rejections_do_not_refund_it(tmp_path):
    cmd,session,http,_,_=rig(tmp_path)
    for number in range(3):
        queue(cmd,uid=number*3+1)
        add_capture(http);http.responses.append(Response(dict(return_code=1)))
        assert cmd.process(session)=='REJECTED'
    assert '3회·20만원' in handle(cmd,'/매수 900001 1 1000',uid=20)
    cmd.disarm()
    replacement=MockTelegramOrders(tmp_path/'mock',confirmed_day=NOW.date(),clock=lambda:NOW)
    replacement.arm(session)
    assert '3회·20만원' in handle(replacement,'/매수 900001 1 1000',uid=21)
    assert len(mutations(http))==3
    replacement.disarm();session.close()


def test_concurrent_confirmations_queue_only_one_order(tmp_path):
    cmd,session,http,_,_=rig(tmp_path)
    preview=handle(cmd,'/매수 900001 1 1000')
    token=re.search('/모의확인 ([A-Za-z0-9_-]+)',preview)[1]
    with ThreadPoolExecutor(max_workers=4) as pool:
        replies=list(pool.map(lambda n:handle(cmd,'/모의확인 '+token,uid=n),range(2,10)))
    assert sum('전송을 요청' in x for x in replies)==1
    assert mutations(http)==[]
    cmd.disarm();session.close()


def test_sell_uses_only_sellable_holdings_and_mock_sell_endpoint(tmp_path):
    cmd,session,http,_,_=rig(tmp_path)
    queue(cmd,'/매도 900001 2 1000')
    add_capture(http);http.responses.append(accepted('0000013'))
    assert cmd.process(session)=='ACKNOWLEDGED'
    assert mutations(http)[0]['headers']['api-id']=='kt10001'
    cmd.disarm();session.close()


def test_final_transport_gate_blocks_stale_capture_after_rate_limit_wait(tmp_path):
    cmd,session,http,_,clock=rig(tmp_path)
    queue(cmd);add_capture(http)
    def pause(delay):
        if cmd._sending: clock[0]+=timedelta(seconds=6)
    session.transport._pause=pause
    assert cmd.process(session)=='UNKNOWN' and mutations(http)==[]
    assert '재주문 금지' in handle(cmd,'/주문상태')
    cmd.disarm();session.close()


def test_interrupted_claim_blocks_reopen_without_resubmission(tmp_path,monkeypatch):
    cmd,session,http,_,_=rig(tmp_path)
    queue(cmd);add_capture(http)
    def interrupted(*args): raise KeyboardInterrupt()
    monkeypatch.setattr(cmd.journal,'dispatch',interrupted)
    with pytest.raises(KeyboardInterrupt): cmd.process(session)
    replacement=MockTelegramOrders(tmp_path/'mock',confirmed_day=NOW.date(),clock=lambda:NOW)
    replacement.arm(session)
    assert '미확인 주문' in handle(replacement,'/매수 900001 1 1000',uid=30)
    assert replacement.process(session) is None and mutations(http)==[]
    replacement.disarm();cmd.disarm();session.close()


def test_daily_notional_limit_blocks_further_orders(tmp_path):
    cmd,session,http,_,_=rig(tmp_path)
    data=wire()
    data[0]['ord_alow_amt']=data[0]['100stk_ord_alow_amt']='200000'
    for n in range(2):
        queue(cmd,'/매수 900001 10 10000',uid=n*3+1)
        add_capture(http,data);http.responses.append(Response(dict(return_code=1)))
        assert cmd.process(session)=='REJECTED'
    assert '3회·20만원' in handle(cmd,'/매수 900001 1 1000',uid=20)
    assert len(mutations(http))==2
    cmd.disarm();session.close()


def test_new_telegram_session_cancels_previous_queued_intent(sandbox,tmp_path):
    from app.telegram_orders import TelegramOrderService
    service,broker,engine,clock=sandbox
    cmd,session,http,_,_=rig(tmp_path)
    queue(cmd)
    # Same owner scope as the pending fixture, mimicking a new receiver session.
    with connect(cmd.path) as db:
        db.execute('UPDATE intents SET scope=?',(service.scope,))
    TelegramOrderService(service.path,engine,11,22,33,{},lambda:clock[0],orders_enabled=False,
                         watch_orders=cmd.handle,watch_cancel_orders=cmd.cancel_session)
    assert cmd.process(session) is None and mutations(http)==[] and broker.calls==[]
    cmd.disarm();session.close()


def test_watch_worker_routes_confirmed_mock_order_on_its_owner_loop(tmp_path,monkeypatch):
    import app.kiwoom_mock_watch_worker as module
    from app.mock_watch_status import MockWatchStatus
    session,client,http,socket,_=setup()
    monkeypatch.setattr(client,'login',lambda *args:None)
    def transport(**kwargs):
        client.profile_id=kwargs['profile_id'];client.cancelled=kwargs['cancelled']
        return client
    monkeypatch.setattr(module,'MockTransport',transport)
    monkeypatch.setattr(module,'MockWatchSession',lambda t:session)
    http.responses.append(Response(dict(return_code=0,acctNo=ACCOUNT)))
    add_capture(http);http.responses.append(accepted('0000013'))
    socket.frames.extend([dict(trnm='LOGIN',return_code=0),dict(trnm='REG',return_code=0)])
    view=MockWatchStatus(clock=lambda:NOW)
    worker=module.MockWatchWorker(tmp_path,'fixture-key','fixture-secret',ACCOUNT,view,
                                 allow_orders=True,confirmed_day=NOW.date(),clock=lambda:NOW)
    worker.verified.connect(lambda report:worker.start_listening())
    def status(text):
        if '알림 받는 중' in text:
            assert worker.orders.armed
            queue(worker.orders)
            assert mutations(http)==[]
    worker.status.connect(status)
    worker.notice.connect(lambda text:worker.stop())
    worker.run()
    assert len(mutations(http))==1 and not worker.orders.armed
    assert worker._credentials==() and client._token is None and http.closed


def test_remote_reconnect_restores_read_only_and_does_not_rearm_orders(tmp_path,monkeypatch):
    import app.kiwoom_mock_watch_worker as module
    from app.mock_watch_status import MockWatchStatus
    rigs=[setup(),setup()]
    for session,client,http,socket,_ in rigs:
        monkeypatch.setattr(client,'login',lambda *args:None)
        http.responses.append(Response(dict(return_code=0,acctNo=ACCOUNT)))
        socket.frames.extend([dict(trnm='LOGIN',return_code=0),dict(trnm='REG',return_code=0)])
    rigs[0][3].frames.append(ConnectionError('fixture disconnect'))
    clients=iter([x[1] for x in rigs])
    def transport(**kwargs):
        client=next(clients);client.profile_id=kwargs['profile_id'];client.cancelled=kwargs['cancelled']
        return client
    monkeypatch.setattr(module,'MockTransport',transport)
    monkeypatch.setattr(module,'MockWatchSession',lambda t:next(x[0] for x in rigs if x[1] is t))
    view=MockWatchStatus(clock=lambda:NOW)
    worker=module.MockWatchWorker(tmp_path,'fixture-key','fixture-secret',ACCOUNT,view,allow_recovery=True,
                                 allow_orders=True,confirmed_day=NOW.date(),clock=lambda:NOW)
    worker.verified.connect(lambda report:worker.start_listening())
    received=[]
    def status(text):
        if '/모의재연결' in text: view.request_reconnect()
        if '알림 받는 중' in text:
            received.append(worker.orders.armed)
            if len(received)==2: worker.stop()
    worker.status.connect(status)
    worker.run()
    assert received==[True,False]
    assert all(mutations(x[2])==[] for x in rigs)
    assert not worker.orders.armed and worker._recovery_credentials==()


def test_confirmation_scope_expiry_cancel_limits_and_closed_day(tmp_path):
    cmd,session,http,_,clock=rig(tmp_path)
    assert '10주' in handle(cmd,'/매수 900001 11 1000')
    assert '쉼표' in handle(cmd,'/매수 900001 1 1,00')
    preview=handle(cmd,'/매도 900001 1 1000')
    token=re.search('/모의확인 ([A-Za-z0-9_-]+)',preview)[1]
    assert '유효한' in handle(cmd,'/모의확인 '+token,scope='other')
    assert '유효한' in handle(cmd,'/모의확인 '+token,session='other')
    assert '철회했어요' in handle(cmd,'/모의철회 '+token,uid=2)
    clock[0]+=timedelta(days=1)
    assert '잠겨' in handle(cmd,'/매수 900001 1 1000',uid=3)
    assert cmd.process(session) is None and not cmd.armed and mutations(http)==[]
    session.close()


def test_telegram_authorization_replay_and_stop_cancels_queue(sandbox,tmp_path):
    service,broker,_,_=sandbox
    cmd,session,http,_,_=rig(tmp_path)
    service.orders_enabled=False
    service.watch_orders,service.watch_cancel_orders=cmd.handle,cmd.cancel_session
    bad=update(sandbox,1,'/매수 900001 1 1000');bad['message']['from']['id']=99
    assert service.handle(bad) is None
    message=update(sandbox,2,'/매수 900001 1 1000')
    preview=service.handle(message)
    assert service.handle(message)==preview
    token=re.search('/모의확인 ([A-Za-z0-9_-]+)',preview)[1]
    confirmation=update(sandbox,3,'/모의확인 '+token)
    assert service.handle(confirmation)==service.handle(confirmation)
    service.handle(update(sandbox,4,'/중지'))
    assert cmd.process(session) is None and mutations(http)==[] and broker.calls==[]
    cmd.disarm();session.close()


@pytest.mark.parametrize('kind',['open','partial','filled','missing'])
def test_fresh_order_observations_distinguish_open_partial_filled_and_unknown(tmp_path,kind):
    cmd,session,http,_,_=rig(tmp_path)
    queue(cmd);add_capture(http);http.responses.append(accepted('0000013'))
    cmd.process(session)
    data=wire()
    for x in data[2]['acnt_ord_cntr_prst_array']: x['ord_no']='0000013'
    if kind in ('open','missing'): data[2]['acnt_ord_cntr_prst_array']=[]
    if kind=='partial': data[2]['acnt_ord_cntr_prst_array']=data[2]['acnt_ord_cntr_prst_array'][1:2]
    if kind in ('open','partial'):
        data[-1]['oso']=[dict(ord_no='13',orig_ord_no='0',stk_cd='900001',ord_qty='3',oso_qty='3' if kind=='open' else '2')]
    add_capture(http,data)
    session.refresh();cmd.observe(session._last_capture)
    result=handle(cmd,'/주문상태')
    expected={'open':'접수·미체결 조회','partial':'부분체결·남은 주문 조회','filled':'체결 수량 조회 일치','missing':'최종 상태 미확인'}
    assert expected[kind] in result and '취소 완료' not in result
    cmd.disarm();session.close()
