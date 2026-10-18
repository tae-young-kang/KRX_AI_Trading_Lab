"""Temporary journals/fake broker only; inspection never mutates a broker."""
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from app.mock_telegram_orders import MockTelegramOrders
from app.order_engine import connect
from test_mock_telegram_orders import rig,queue,handle,add_capture,mutations
from test_mock_telegram_actions import ready,snapshot,refresh,action
from test_kiwoom_mock_dispatch import NOW,accepted
from test_telegram_orders import sandbox,update


def test_missing_record_latches_cancels_pending_and_survives_good_data_and_restart(tmp_path):
    cmd,session,http,_,_,data=ready(tmp_path)
    queue(cmd,'/매수 900002 1 1000',uid=20)
    good=session._last_capture
    missing=replace(good,observations=replace(good.observations,executions=()))
    cmd.observe(missing)
    assert cmd.needs_review and not cmd.armed and cmd.process(session) is None
    text=handle(cmd,'/주문점검')
    assert '누락되거나' in text and '다시 켜도 차단을 유지해요' in text
    with connect(cmd.path) as db:
        assert db.execute('SELECT state FROM intents ORDER BY rowid DESC LIMIT 1').fetchone()[0]=='CANCELED'
    cmd.observe(good)
    assert cmd.needs_review and not cmd.armed
    cmd.disarm()
    reopened=MockTelegramOrders(tmp_path/'mock',confirmed_day=NOW.date(),clock=lambda:NOW)
    reopened.arm(session)
    assert reopened.needs_review and not reopened.armed and not reopened.can_dispatch()
    assert '충돌' in handle(reopened,'/매수 900001 1 1000',uid=30)
    assert '누락되거나' in handle(reopened,'/주문점검') and len(mutations(http))==1
    reopened.disarm();session.close()


def test_conflicting_order_quantity_blocks_and_reason_has_no_payload(tmp_path):
    cmd,session,http,_,_=rig(tmp_path)
    queue(cmd);add_capture(http);http.responses.append(accepted('0000013'))
    cmd.process(session)
    refresh(cmd,session,http,snapshot(filled=4,remaining=0))
    assert cmd.needs_review and not cmd.armed
    text=handle(cmd,'/주문점검')
    assert '종목·방향·수량' in text
    assert '900001' not in text and '0000013' not in text and 'fixture' not in text
    assert '해제를 실행하지 않아요' in text
    cmd.disarm();session.close()


def test_context_conflict_reason_is_fixed_and_cannot_be_overwritten(tmp_path):
    cmd,session,http,_,_,_=ready(tmp_path)
    cmd.observe(replace(session._last_capture,profile_id='f'*32))
    cmd.observe(replace(session._last_capture,observations=replace(session._last_capture.observations,executions=())))
    text=handle(cmd,'/주문점검')
    assert '조회 계좌·인증·날짜' in text and 'f'*32 not in text
    assert '누락되거나' not in text and cmd.needs_review
    cmd.disarm();session.close()


def test_missing_but_not_contradictory_observation_waits_for_a_new_matching_query(tmp_path):
    cmd,session,http,_,_=rig(tmp_path)
    queue(cmd);add_capture(http);http.responses.append(accepted('0000013'))
    cmd.process(session)
    refresh(cmd,session,http,snapshot(filled=0,remaining=0))
    assert not cmd.needs_review
    assert '대조 대기 1건' in handle(cmd,'/주문점검')
    assert '새 조회 대조' in handle(cmd,'/매수 900002 1 1000',uid=30)
    refresh(cmd,session,http,snapshot(filled=3,remaining=0))
    assert '대조 대기 0건' in handle(cmd,'/주문점검')
    assert '/모의확인' in handle(cmd,'/매수 900002 1 1000',uid=31)
    assert len(mutations(http))==1
    cmd.disarm();session.close()


def test_preflight_itself_detects_loss_before_next_mutation(tmp_path):
    cmd,session,http,_,_,_=ready(tmp_path)
    queue(cmd,'/매수 900002 1 1000',uid=20)
    add_capture(http,snapshot(filled=0,remaining=0))
    assert cmd.process(session)=='FAILED' and cmd.needs_review
    assert len(mutations(http))==1
    cmd.disarm();session.close()


def test_corrupt_amendment_lineage_is_latched(tmp_path):
    cmd,session,http,_,_,data=ready(tmp_path)
    action(cmd,'/정정 13 2 1100');add_capture(http,data);http.responses.append(accepted('0000014','0000013'))
    cmd.process(session)
    with connect(cmd.journal.path) as db:
        db.execute("UPDATE mock_dispatch SET base_order_no='0000099' WHERE api_id='kt10002'")
    refresh(cmd,session,http,snapshot(order='14',original='13',quantity=2,remaining=2))
    assert cmd.needs_review and '원주문 연결' in handle(cmd,'/주문점검')
    assert len(mutations(http))==2
    cmd.disarm();session.close()


def test_cumulative_amendment_fill_cannot_exceed_confirmed_target(tmp_path):
    cmd,session,http,_,_,data=ready(tmp_path)
    action(cmd,'/정정 13 2 1100');add_capture(http,data);http.responses.append(accepted('0000014','0000013'))
    cmd.process(session)
    later=snapshot(filled=2,remaining=0)
    parent=later[2]['acnt_ord_cntr_prst_array'][0]
    later[2]['acnt_ord_cntr_prst_array'].append(dict(parent,ord_no='0000014',orig_ord_no='0000013',
                                                  cntr_no='0000003',cntr_qty='2',cntr_uv='1100'))
    refresh(cmd,session,http,later)
    assert cmd.needs_review and '종목·방향·수량' in handle(cmd,'/주문점검')
    assert len(mutations(http))==2 and not cmd.can_dispatch()
    cmd.disarm();session.close()


def test_unknown_dispatch_and_cancel_finality_are_distinct_from_conflict(tmp_path):
    cmd,session,http,_,_=rig(tmp_path)
    queue(cmd);add_capture(http);http.responses.append(TimeoutError('PRIVATE'))
    cmd.process(session)
    text=handle(cmd,'/주문점검')
    assert '미확인 전송' in text and '결과 미확인 1건' in text and 'PRIVATE' not in text
    assert not cmd.needs_review
    cmd.disarm();session.close()


def test_cancel_finality_remains_blocked_even_after_observation_review(tmp_path):
    cmd,session,http,_,_,data=ready(tmp_path)
    action(cmd,'/취소 13');add_capture(http,data);http.responses.append(accepted('0000014','0000013'))
    cmd.process(session)
    refresh(cmd,session,http,snapshot(remaining=0))
    text=handle(cmd,'/주문점검')
    assert '최종 취소 대조 대기 1건' in text and '취소 접수는 완료가 아니에요' in text
    assert not cmd.needs_review and '최종 상태 대조' in handle(cmd,'/매수 900002 1 1000',uid=30)
    cmd.disarm();session.close()


def test_inspection_does_not_wait_for_owner_network_work(tmp_path,monkeypatch):
    cmd,session,http,_,_=rig(tmp_path)
    queue(cmd);add_capture(http);http.responses.append(accepted('0000013'))
    entered,release=Event(),Event()
    original=session.capture_for_order
    def capture():
        entered.set()
        assert release.wait(10)
        return original()
    monkeypatch.setattr(session,'capture_for_order',capture)
    with ThreadPoolExecutor(max_workers=2) as pool:
        processing=pool.submit(cmd.process,session)
        try:
            assert entered.wait(3)
            report=pool.submit(handle,cmd,'/주문점검').result(timeout=3)
            assert '처리 중 1건' in report
        finally:
            release.set()
        assert processing.result(timeout=3)=='ACKNOWLEDGED'
    assert len(mutations(http))==1
    cmd.disarm();session.close()


def test_telegram_inspection_authorization_and_read_only_after_stop(sandbox,tmp_path):
    service,broker,_,_=sandbox
    cmd,session,http,_,_=rig(tmp_path)
    service.orders_enabled=False
    service.watch_orders,service.watch_cancel_orders=cmd.handle,cmd.cancel_session
    bad=update(sandbox,1,'/주문점검');bad['message']['from']['id']=99
    assert service.handle(bad) is None
    message=update(sandbox,2,'/주문점검')
    reply=service.handle(message)
    assert service.handle(message)==reply and '현재 저장된 차단 없음' in reply
    service.handle(update(sandbox,3,'/중지'))
    assert '모의 주문 점검' in service.handle(update(sandbox,4,'/주문점검'))
    assert mutations(http)==[] and broker.calls==[]
    cmd.disarm();session.close()
