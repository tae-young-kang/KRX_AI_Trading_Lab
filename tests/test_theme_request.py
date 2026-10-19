"""Cached Theme1 Telegram requests, temporary DB and synthetic bars only."""
from copy import deepcopy
from datetime import date,datetime
from app.trading_clock import KST
from app.theme_calendar import request_selection
import pytest
from app.krx_history import HistoryStore
from app.theme_input import prepare_confirmed, saved_selection
from app.theme_request import ThemeRequest
from test_theme_input import selection
from test_telegram_orders import sandbox, update


def setup(tmp_path):
    history=HistoryStore(tmp_path/'history.db')
    data=request_selection('REAL_QUOTES',now=datetime(2026,10,19,18,tzinfo=KST))
    prepare_confirmed(history,data,'REAL_QUOTES',today=date(2026,10,19))
    clock=[0]
    request=ThemeRequest(history,'REAL_QUOTES',clock=lambda:clock[0],wall_clock=lambda:datetime(2026,10,19,18,tzinfo=KST))
    return history,data,clock,request


def test_selection_survives_restart_and_request_freezes_confirmed_dates(tmp_path):
    history,data,clock,request=setup(tmp_path)
    assert saved_selection(HistoryStore(history.path),'REAL_QUOTES')==data
    assert data['through'] in request.request()
    assert '이미' in request.request()
    # A later PC selection must not replace the dates of an already queued request.
    newer=deepcopy(data)
    newer['source']='CSV_USER'
    prepare_confirmed(history,newer,'REAL_QUOTES',today=date(2026,10,19))
    assert request.take('REAL_QUOTES',busy=True,active=True) is None
    frozen=request.take('REAL_QUOTES',busy=False,active=True)
    assert frozen==data
    result=prepare_confirmed(history,frozen,'REAL_QUOTES',remember=False)
    assert saved_selection(history,'REAL_QUOTES')==newer
    request.finish(success=True,result=result)
    assert '자료 부족' in request.summary() and data['through'] in request.summary()
    assert request.state=='NEEDS_DATA' and '없다는 뜻이 아니에요' in request.summary()


def test_missing_settings_expiry_mode_and_stop_fail_without_execution(tmp_path):
    history,data,clock,request=setup(tmp_path)
    other=ThemeRequest(history,'MOCK')
    assert '테마1 판정 요청' in other.request()
    request.request();clock[0]=120
    assert request.take('REAL_QUOTES',busy=False,active=True) is None
    assert '만료' in request.summary()
    request.request()
    assert request.take('MOCK',busy=False,active=True) is None
    assert '모드' in request.summary()
    request.request()
    assert request.take('REAL_QUOTES',busy=False,active=False) is None
    assert '수신' in request.summary()
    with pytest.raises(ValueError):saved_selection(history,'MOCK')


def test_authorized_commands_replay_stop_and_no_orders(sandbox,tmp_path):
    history,data,clock,request=setup(tmp_path)
    service,broker,_,_=sandbox
    service.orders_enabled=False
    service.theme_request=request
    bad=update(sandbox,1,'/테마1검색');bad['message']['from']['id']=99
    assert service.handle(bad) is None
    event=update(sandbox,2,'/테마1검색')
    reply=service.handle(event)
    assert reply==service.handle(event) and data['through'] in reply
    assert '이미' in service.handle(update(sandbox,3,'/테마1검색'))
    assert '계좌 주문은 실행하지 않아요' in service.handle(update(sandbox,4,'/테마1검색상태'))
    service.handle(update(sandbox,5,'/중지'))
    assert '중지' in service.handle(update(sandbox,6,'/테마1검색'))
    assert request.take('REAL_QUOTES',busy=False,active=service.accepts_search_requests()) is None
    assert broker.calls==[]


def test_gui_timer_hands_theme_to_cached_engine_without_market_scan(tmp_path):
    from types import SimpleNamespace
    from test_recommendation_request import gui_method
    from app.recommendation_request import RecommendationRequest
    history,data,clock,request=setup(tmp_path)
    request.request()
    calls=[]
    fake=SimpleNamespace(remote_scan=RecommendationRequest('REAL_QUOTES'),remote_scan_active=False,
        remote_theme=request,remote_theme_active=False,work=None,cfg={},history=history,
        telegram_order_worker=SimpleNamespace(isRunning=lambda:True,isInterruptionRequested=lambda:False,
            service=SimpleNamespace(accepts_search_requests=lambda:True)),
        scan=lambda **kwargs:calls.append('unexpected market scan'))
    fake.remote_theme_done=lambda result:gui_method('remote_theme_done')(fake,result)
    fake.run_task=lambda fn,ok,text: (calls.append('cached task'),ok(fn()))
    gui_method('poll_remote_scan',current_mode=lambda cfg:SimpleNamespace(key='REAL_QUOTES'),
               prepare_confirmed=prepare_confirmed)(fake)
    assert calls==['cached task'] and not fake.remote_theme_active
    assert '자료 부족' in request.summary() and request.state=='NEEDS_DATA'
