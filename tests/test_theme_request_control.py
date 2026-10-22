"""Offline Theme1 progress/cancel handoff; no GUI construction or clients."""
from types import SimpleNamespace
import pytest
from app.theme_request import ThemeRequest
from app.theme_kiwoom import collect_theme
from app.theme_delivery import latest_snapshot
from test_theme_kiwoom import Client,NOW,history_with_baseline
from test_recommendation_request import gui_method
from test_telegram_orders import sandbox,update


def rig(tmp_path):
    history=history_with_baseline(tmp_path);clock=[0.0]
    request=ThemeRequest(history,'REAL_QUOTES',clock=lambda:clock[0],wall_clock=lambda:NOW)
    request.request()
    return history,request,clock


def test_pending_cancel_prevents_claim_and_new_request_resets_flag(tmp_path):
    history,request,clock=rig(tmp_path)
    assert '대기 중인 테마1 검색을 취소' in request.cancel()
    assert request.state=='CANCELED' and request.take('REAL_QUOTES',busy=False,active=True) is None
    request.request();request.take('REAL_QUOTES',busy=False,active=True)
    assert request.state=='RUNNING' and not request.cancellation_requested()


def test_progress_is_sanitized_elapsed_is_shown_and_cancellation_stays_visible(tmp_path):
    _,request,clock=rig(tmp_path);request.take('REAL_QUOTES',busy=False,active=True)
    request.progress('테마1 일봉 확인 12/200\n\x00'+'x'*400)
    clock[0]=125
    text=request.summary()
    assert '12/200' in text and '\x00' not in text and '2분 5초' in text and 'x'*241 not in text
    request.cancel();request.progress('late update')
    assert request.cancellation_requested() and '취소 요청' in request.summary() and 'late update' not in request.summary()


def test_running_cancel_aborts_collection_keeps_previous_run_and_sends_only_cancel_notice(tmp_path):
    history,request,_=rig(tmp_path)
    previous=collect_theme(history,Client(),'REAL_QUOTES',now=NOW)
    request.take('REAL_QUOTES',busy=False,active=True)
    class CancelingClient(Client):
        def query(self,*args):
            result=super().query(*args);request.cancel();return result
    with pytest.raises(ValueError,match='중단'):
        collect_theme(history,CancelingClient(),'REAL_QUOTES',now=NOW,
            progress=request.progress,cancelled=request.cancellation_requested)
    request.finish(success=False)
    assert request.state=='CANCELED' and latest_snapshot(history,'REAL_QUOTES')['run_id']==previous['run_id']
    class Sender:
        def __init__(self):self.sent=[]
        def send(self,chat,text):self.sent.append(text);return len(self.sent)
    sender=Sender();request.send_notice(sender,22,active=True)
    request.send_notice(sender,22,active=True)
    assert len(sender.sent)==1 and '중단 처리를 마쳤어요' in sender.sent[0]
    assert '[테마1 · 종목 감지]' not in sender.sent[0]


def test_late_cancel_does_not_claim_already_saved_result_was_canceled(tmp_path):
    history,request,_=rig(tmp_path);request.take('REAL_QUOTES',busy=False,active=True)
    result=collect_theme(history,Client(),'REAL_QUOTES',now=NOW)
    request.cancel();request.finish(success=True,result=result)
    assert request.state=='DONE' and '이미 저장됐어요' in request.summary()


def test_gui_progress_and_cancel_poll_route_to_the_owned_theme_task_without_gui(tmp_path):
    _,request,_=rig(tmp_path);request.take('REAL_QUOTES',busy=False,active=True)
    widget=SimpleNamespace(setText=lambda text:None)
    fake=SimpleNamespace(remote_theme_active=True,remote_theme=request,remote_scan_active=False,
        count_info=widget,theme_progress=widget,statusBar=lambda:SimpleNamespace(showMessage=lambda text:None))
    gui_method('show_scan_progress')(fake,'테마1 일봉 확인 1/100')
    assert '1/100' in request.summary()
    calls=[];request.cancel();fake.telegram_order_worker=None
    fake.work=SimpleNamespace(isRunning=lambda:True,requestInterruption=lambda:calls.append(True))
    gui_method('poll_remote_scan')(fake)
    assert calls==[True]


def test_private_telegram_cancel_and_status_never_place_orders(tmp_path,sandbox):
    _,request,clock=rig(tmp_path);service,broker,_,_=sandbox;service.theme_request=request
    request.take('REAL_QUOTES',busy=False,active=True);request.progress('일봉 확인 2/100')
    bad=update(sandbox,1,'/테마1검색취소');bad['message']['from']['id']=99
    assert service.handle(bad) is None and not request.cancellation_requested()
    assert '2/100' in service.handle(update(sandbox,2,'/테마1검색상태'))
    assert '취소 요청' in service.handle(update(sandbox,3,'/테마1검색취소'))
    assert request.cancellation_requested() and broker.calls==[]
