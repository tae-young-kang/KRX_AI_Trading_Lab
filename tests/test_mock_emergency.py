"""Emergency stop/resume uses only fake IO and temporary databases."""
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import re
import pytest
from app.mock_telegram_orders import MockTelegramOrders
from test_mock_telegram_orders import rig,handle,queue,add_capture,mutations
from test_mock_telegram_actions import ready,action,snapshot
from test_kiwoom_mock_dispatch import NOW,accepted
from test_telegram_orders import sandbox,update


def fresh_after_stop(cmd,session,http,clock,data=None):
    clock[0]+=timedelta(seconds=1)
    add_capture(http,data)
    session.refresh();cmd.observe(session._last_capture)


def resume_token(cmd,scope='owner',session='receiver'):
    reply=handle(cmd,'/모의재개',uid=40,scope=scope,session=session)
    return re.search('/모의재개확인 ([A-Za-z0-9_-]+)',reply)[1]


def test_stop_cancels_queue_and_only_fresh_explicit_confirmation_resumes(tmp_path):
    cmd,session,http,_,clock=rig(tmp_path)
    old=queue(cmd)
    reply=handle(cmd,'/긴급중지',uid=3)
    assert '접수된 주문을 취소한 것은 아니에요' in reply
    assert cmd.emergency.event.is_set() and cmd.process(session) is None
    assert '긴급중지' in handle(cmd,'/주문점검')
    assert '/잔고 새 조회' in handle(cmd,'/모의재개',uid=4)
    fresh_after_stop(cmd,session,http,clock)
    token=resume_token(cmd)
    assert '해제했어요' in handle(cmd,'/모의재개확인 '+token,uid=41)
    assert cmd.armed and not cmd.emergency.event.is_set() and mutations(http)==[]
    assert '유효한' in handle(cmd,'/모의확인 '+old,uid=42)
    assert '상태가 아니에요' in handle(cmd,'/모의재개확인 '+token,uid=43)
    assert cmd.process(session) is None
    cmd.disarm();session.close()


def test_restart_and_local_rearm_preserve_stop_and_invalidate_resume_code(tmp_path):
    cmd,session,http,_,clock=rig(tmp_path)
    handle(cmd,'/긴급중지');fresh_after_stop(cmd,session,http,clock)
    token=resume_token(cmd)
    cmd.disarm()
    reopened=MockTelegramOrders(tmp_path/'mock',confirmed_day=NOW.date(),clock=lambda:clock[0])
    reopened.arm(session)
    assert reopened.emergency.event.is_set() and not reopened.armed
    fresh_after_stop(reopened,session,http,clock)
    assert '유효한 재개 확인이 없어요' in handle(reopened,'/모의재개확인 '+token)
    assert mutations(http)==[]
    reopened.disarm();session.close()


@pytest.mark.parametrize('change',['scope','receiver','expiry','query_changed','new_stop','relogin','receiver_stopped'])
def test_resume_code_is_bound_to_current_owner_session_capture_and_time(tmp_path,change):
    cmd,session,http,_,clock=rig(tmp_path)
    handle(cmd,'/긴급중지');fresh_after_stop(cmd,session,http,clock)
    token=resume_token(cmd)
    scope,receiver='owner','receiver'
    if change=='scope': scope='other'
    elif change=='receiver': receiver='other'
    elif change=='expiry': clock[0]+=timedelta(seconds=31)
    elif change=='query_changed': fresh_after_stop(cmd,session,http,clock)
    elif change=='new_stop':
        handle(cmd,'/긴급중지');fresh_after_stop(cmd,session,http,clock)
    elif change=='relogin': session.transport._auth_generation='f'*32
    elif change=='receiver_stopped': cmd.cancel_session('owner','receiver')
    response=handle(cmd,'/모의재개확인 '+token,scope=scope,session=receiver)
    assert '해제했어요' not in response and cmd.emergency.event.is_set() and not cmd.armed
    assert mutations(http)==[]
    cmd.disarm();session.close()


def test_unknown_transmission_cannot_be_unlocked_by_manual_resume(tmp_path):
    cmd,session,http,_,clock=rig(tmp_path)
    queue(cmd);add_capture(http);http.responses.append(TimeoutError('PRIVATE'))
    assert cmd.process(session)=='UNKNOWN'
    handle(cmd,'/긴급중지');fresh_after_stop(cmd,session,http,clock)
    assert '미확인 처리' in handle(cmd,'/모의재개')
    assert cmd.emergency.event.is_set() and len(mutations(http))==1
    cmd.disarm();session.close()


def test_final_cancel_and_record_conflict_cannot_be_unlocked(tmp_path):
    cmd,session,http,_,clock,data=ready(tmp_path)
    action(cmd,'/취소 13');add_capture(http,data);http.responses.append(accepted('0000014','0000013'))
    cmd.process(session)
    handle(cmd,'/긴급중지');fresh_after_stop(cmd,session,http,clock,snapshot(remaining=0))
    assert '대조 대기' in handle(cmd,'/모의재개')
    cmd._latch('ORDER_CONFLICT')
    assert '기록 충돌' in handle(cmd,'/모의재개') and cmd.needs_review
    assert cmd.emergency.event.is_set() and len(mutations(http))==2
    cmd.disarm();session.close()


def test_stop_returns_during_preflight_and_prevents_mutation(tmp_path,monkeypatch):
    cmd,session,http,_,_=rig(tmp_path)
    queue(cmd);add_capture(http)
    entered,release=Event(),Event()
    original=session.capture_for_order
    def capture():
        entered.set()
        assert release.wait(10)
        return original()
    monkeypatch.setattr(session,'capture_for_order',capture)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending=pool.submit(cmd.process,session)
        try:
            assert entered.wait(3)
            text=pool.submit(handle,cmd,'/긴급중지').result(timeout=3)
            assert '새 전송을 차단' in text and cmd.emergency.event.is_set()
        finally: release.set()
        assert pending.result(timeout=3)=='FAILED'
    assert mutations(http)==[]
    cmd.disarm();session.close()


def test_stop_at_final_rate_limit_gate_blocks_http_without_retry(tmp_path):
    cmd,session,http,_,_=rig(tmp_path)
    queue(cmd);add_capture(http)
    def pause(delay):
        if cmd._sending: handle(cmd,'/긴급중지')
    session.transport._pause=pause
    assert cmd.process(session)=='UNKNOWN' and mutations(http)==[]
    assert cmd.emergency.event.is_set()
    cmd.disarm();session.close()


def test_already_posted_order_is_not_falsely_reported_as_canceled(tmp_path,monkeypatch):
    cmd,session,http,_,_=rig(tmp_path)
    queue(cmd);add_capture(http);http.responses.append(accepted('0000013'))
    original=http.post
    def post(url,**kwargs):
        result=original(url,**kwargs)
        if kwargs['headers'].get('api-id')=='kt10000': handle(cmd,'/긴급중지')
        return result
    monkeypatch.setattr(http,'post',post)
    assert cmd.process(session)=='ACKNOWLEDGED' and len(mutations(http))==1
    assert cmd.emergency.event.is_set() and '접수 응답 확인' in handle(cmd,'/주문상태')
    cmd.disarm();session.close()


def test_new_stop_during_resume_wins_over_permission(tmp_path,monkeypatch):
    cmd,session,http,_,clock=rig(tmp_path)
    handle(cmd,'/긴급중지');fresh_after_stop(cmd,session,http,clock)
    token=resume_token(cmd)
    original=cmd.emergency.confirm
    def confirm(*args):
        result=original(*args)
        handle(cmd,'/긴급중지')
        return result
    monkeypatch.setattr(cmd.emergency,'confirm',confirm)
    assert '중지를 유지' in handle(cmd,'/모의재개확인 '+token)
    assert cmd.emergency.event.is_set() and not cmd.armed and not cmd.can_dispatch()
    cmd.disarm();session.close()


def test_telegram_auth_replay_and_mode_separation(sandbox,tmp_path):
    service,broker,_,_=sandbox
    cmd,session,http,_,clock=rig(tmp_path)
    service.orders_enabled=False
    service.watch_orders,service.watch_cancel_orders=cmd.handle,cmd.cancel_session
    bad=update(sandbox,1,'/긴급중지');bad['message']['from']['id']=99
    assert service.handle(bad) is None and not cmd.emergency.event.is_set()
    message=update(sandbox,2,'/긴급중지')
    reply=service.handle(message)
    stopped=cmd.emergency.stopped_at()
    clock[0]+=timedelta(seconds=1)
    assert service.handle(message)==reply and cmd.emergency.stopped_at()==stopped
    service.handle(update(sandbox,3,'/중지'))
    assert '키움 모의 긴급중지' in service.handle(update(sandbox,4,'/긴급중지'))
    assert '중지됐거나' in service.handle(update(sandbox,5,'/모의재개'))
    assert mutations(http)==[] and broker.calls==[]
    cmd.disarm();session.close()
