"""Captured cancellation notices and explicit fake queries, no live IO."""
from dataclasses import replace
from datetime import timedelta
import json
import pytest
from app.order_engine import connect
from app.kiwoom_mock_contract import ContractError
from test_mock_telegram_actions import ready,action,snapshot
from test_mock_telegram_orders import add_capture,mutations,handle
from test_kiwoom_mock_dispatch import accepted
from test_kiwoom_mock_stream import real
from test_kiwoom_mock_status import event


def capture_notice(tmp_path,status='취소'):
    cmd,session,http,socket,clock,data=ready(tmp_path)
    action(cmd,'/취소 13')
    add_capture(http,data);http.responses.append(accepted('0000014','0000013'))
    assert cmd.process(session)=='ACKNOWLEDGED'
    clock[0]+=timedelta(seconds=1)
    message=event(**{'9203':'14','904':'13','908':clock[0].strftime('%H%M%S'),'913':status})
    socket.frames.append(real(message))
    calls=len(http.calls)
    assert session.poll() and len(http.calls)==calls
    notice=session.cancellation_notices()[0]
    clock[0]+=timedelta(seconds=1)
    return cmd,session,http,socket,clock,notice,message


def records(cmd):
    with connect(cmd.ledger.path) as db:
        return db.execute('SELECT * FROM cancel_review_events').fetchall()


def latest(cmd):
    with connect(cmd.path) as db:
        row=db.execute('SELECT * FROM intents ORDER BY rowid DESC LIMIT 1').fetchone()
        return json.loads(row['payload'])['root_key']


@pytest.mark.parametrize('status,remaining,title',[
    ('취소',0,'조회된 남은 주문은 없어요'),('취소',2,'남은 주문이 있어요'),
    ('거부',2,'취소 요청이 거절됐어요'),('접수',2,'취소 확인 중'),
])
def test_real_notice_then_explicit_capture_is_reviewed_without_extra_http(tmp_path,status,remaining,title):
    cmd,session,http,_,_,notice,_=capture_notice(tmp_path,status)
    add_capture(http,snapshot(remaining=remaining))
    session.refresh()
    before=len(http.calls)
    cmd.observe(session._last_capture,stream=session.stream,notices=session.cancellation_notices())
    text=handle(cmd,'/주문상태')
    assert '취소 알림 뒤 새 조회 대조' in text and title in text
    assert '최종 취소 미확인' in text
    assert len(http.calls)==before and len(mutations(http))==2
    saved=records(cmd)
    assert len(saved)==1 and saved[0]['remaining_quantity']==remaining and saved[0]['filled_quantity']==1
    assert '최종 상태 대조' in handle(cmd,'/매수 900001 1 1000',uid=30)
    cmd.disarm();session.close()


@pytest.mark.parametrize('change',['before_notice','wrong_session','wrong_account','relogin','stale','closed'])
def test_invalid_pair_cannot_write_a_successful_review(tmp_path,change):
    cmd,session,http,socket,clock,notice,_=capture_notice(tmp_path)
    add_capture(http,snapshot(remaining=0));session.refresh()
    captured=session._last_capture
    if change=='before_notice': captured=replace(captured,started_at=notice.received_at)
    elif change=='wrong_session': notice=replace(notice,session='e'*32)
    elif change=='wrong_account': captured=replace(captured,observations=replace(captured.observations,account_tag='f'*64))
    elif change=='relogin': session.transport._auth_generation='e'*32
    elif change=='stale': clock[0]+=timedelta(seconds=121)
    elif change=='closed': socket.closed=True
    before=len(http.calls)
    with pytest.raises(ContractError):
        session.stream.review_captured_cancellation(cmd.journal,cmd.ledger,latest(cmd),notice,captured,known_orders={})
    assert records(cmd)==[] and len(http.calls)==before and len(mutations(http))==2
    cmd.disarm();session.close()


def test_connection_loss_during_review_cannot_leave_success_record(tmp_path,monkeypatch):
    cmd,session,http,socket,_,notice,_=capture_notice(tmp_path)
    add_capture(http,snapshot(remaining=0));session.refresh()
    original=cmd.ledger.ingest
    def ingest(observed):
        result=original(observed)
        socket.closed=True
        return result
    monkeypatch.setattr(cmd.ledger,'ingest',ingest)
    with pytest.raises(ContractError):
        session.stream.review_captured_cancellation(cmd.journal,cmd.ledger,latest(cmd),notice,session._last_capture,known_orders={})
    assert records(cmd)==[] and len(mutations(http))==2
    cmd.disarm();session.close()


def test_review_is_idempotent_and_never_proves_finality(tmp_path):
    cmd,session,http,_,_,notice,_=capture_notice(tmp_path)
    add_capture(http,snapshot(remaining=0));session.refresh()
    for _ in range(2):
        review=session.stream.review_captured_cancellation(cmd.journal,cmd.ledger,latest(cmd),notice,
                                                         session._last_capture,known_orders={})
        assert not review.final_cancellation_proven and not review.ready_for_orders
    assert len(records(cmd))==1 and len(mutations(http))==2
    cmd.disarm();session.close()


def test_notice_cache_is_bounded_deduplicated_and_cleared_on_close(tmp_path):
    cmd,session,http,socket,clock,_,message=capture_notice(tmp_path)
    socket.frames.append(real(message))
    assert session.poll()==[] and len(session.cancellation_notices())==1
    for number in range(15,40):
        socket.frames.append(real(event(**{'9203':str(number),'904':'13','908':clock[0].strftime('%H%M%S')})))
        session.poll()
    assert len(session.cancellation_notices())==20
    session.close()
    assert len(session._cancel_notices)==0
    cmd.disarm()
