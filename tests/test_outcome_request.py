from datetime import datetime,timedelta
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from app.outcome_request import OutcomeRequest
from app.trading_clock import KST
from test_telegram_orders import sandbox,update


def test_request_freezes_source_date_and_single_claim():
    source=['CSV_USER'];now=[datetime(2026,10,19,23,59,tzinfo=KST)]
    request=OutcomeRequest('REAL_QUOTES',source=lambda:source[0],wall_clock=lambda:now[0])
    request.request();source[0]='KRX';now[0]+=timedelta(minutes=2)
    assert request.take('REAL_QUOTES',busy=True,active=True) is None
    with ThreadPoolExecutor(max_workers=3) as pool:
        claims=list(pool.map(lambda _:request.take('REAL_QUOTES',busy=False,active=True),range(3)))
    claimed=[r for r in claims if r is not None]
    assert len(claimed)==1 and claimed[0]['source']=='CSV_USER'
    assert claimed[0]['today'].isoformat()=='2026-10-19'
    request.finish(success=True,result=dict(recommendations=3,added=2,available_through='2026-10-16'))
    calls=[]
    def uncertain(text):calls.append(text);raise TimeoutError()
    request.send_notice(uncertain,active=True);request.send_notice(uncertain,active=True)
    assert len(calls)==1 and '미확인' in request.summary()


def test_expiry_mode_change_and_stopped_receiver():
    clock=[0];request=OutcomeRequest('MOCK',clock=lambda:clock[0])
    request.request();clock[0]=120
    assert request.take('MOCK',busy=False,active=True) is None
    assert '만료' in request.summary()
    request.request()
    assert request.take('REAL_QUOTES',busy=False,active=True) is None
    request.request()
    assert request.take('MOCK',busy=False,active=False) is None
    assert '가상 연습' in OutcomeRequest('DEMO').request()


def test_authorized_command_replay_and_stop_never_call_broker(sandbox):
    service,broker,_,_=sandbox;service.orders_enabled=False
    request=OutcomeRequest('MOCK');service.outcome_request=request
    bad=update(sandbox,1,'/성적갱신');bad['message']['from']['id']=99
    assert service.handle(bad) is None
    event=update(sandbox,2,'/성적갱신')
    reply=service.handle(event)
    assert reply==service.handle(event) and '요청했어요' in reply
    assert '이미' in service.handle(update(sandbox,3,'/성적갱신'))
    assert '추천 성적 갱신' in service.handle(update(sandbox,4,'/성적갱신상태'))
    service.handle(update(sandbox,5,'/중지'))
    assert '중지' in service.handle(update(sandbox,6,'/성적갱신'))
    assert broker.calls==[]


def test_gui_handoff_evaluates_temporary_cache_and_returns_completion(tmp_path):
    from test_recommendation_request import gui_method
    from test_recommendation_reports import store
    from app.krx_history import HistoryStore
    db,_=store(tmp_path);history=HistoryStore(tmp_path/'history.db')
    with db.connect() as conn:conn.execute("UPDATE recommendation_batches SET mode='REAL_QUOTES'")
    request=OutcomeRequest('REAL_QUOTES',source=lambda:'CSV_USER',
        wall_clock=lambda:datetime(2026,10,20,10,tzinfo=KST))
    request.request()
    fake=SimpleNamespace(db=db,history=history,cfg={},work=None,remote_scan_active=False,
        remote_scan=SimpleNamespace(take=lambda *args,**kwargs:False),remote_theme=None,
        remote_outcome=request,remote_outcome_active=False,
        telegram_order_worker=SimpleNamespace(isRunning=lambda:True,isInterruptionRequested=lambda:False,
            service=SimpleNamespace(accepts_search_requests=lambda:True)))
    fake.remote_outcome_done=lambda result:gui_method('remote_outcome_done')(fake,result)
    fake.run_task=lambda fn,done,text:done(fn())
    gui_method('poll_remote_scan',current_mode=lambda cfg:SimpleNamespace(key='REAL_QUOTES'))(fake)
    assert request.state=='DONE' and not fake.remote_outcome_active
    messages=[]
    request.send_notice(lambda text:messages.append(text) or 1,active=True)
    assert len(messages)==1 and '추천 3개 확인' in messages[0]
    assert '자료 확인일 2026-10-19' in messages[0]
