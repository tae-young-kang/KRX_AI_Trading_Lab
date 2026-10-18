"""Offline observation reporting cannot authorize or certify executions."""
import pytest
from app.mock_watch_status import MockWatchStatus
from test_kiwoom_mock_status import NOW
from test_telegram_orders import sandbox, update


def report():
    return dict(checked_at='10/19 10:00:00',order_money=100,held=1,pending=0,
                account='PRIVATE_ACCOUNT',access_token='PRIVATE_TOKEN')


def test_observations_do_not_become_execution_proof_or_expose_payload():
    view = MockWatchStatus(clock=lambda:NOW)
    assert '아직 기록이 없어요' in view.validation_summary()
    view.reset()
    view.verified(report())
    view.enable_refresh()
    view.enable_refresh()  # Quiet polling must not flood the bounded log.
    view.notice('PRIVATE_BROKER_PAYLOAD')
    view.finish_refresh(report())
    text = view.validation_summary()
    assert '새 잔고 조회 1회 · 주문 처리 알림 1회' in text
    assert text.count('모의 알림 등록 확인 관측') == 1
    assert 'PRIVATE' not in text and '100원' not in text
    assert '체결 완료·잔고 일치 증명이 아니에요' in text
    assert '실계좌 주문 허용을 판정하지 않아요' in text
    view.stopped()
    assert '알림 등록: 연결 전 또는 종료' in view.validation_summary()
    view.reset()
    assert '조회 0회 · 주문 처리 알림 0회' in view.validation_summary()
    assert '계좌·잔고 조회: 아직 관측 없음' in view.validation_summary()


def test_trace_is_bounded_and_not_persisted_across_new_status_instance():
    view = MockWatchStatus(clock=lambda:NOW)
    for _ in range(50): view.notice('PRIVATE')
    assert len(view._validation.events) == 20
    assert view.validation_summary().count('10/19 10:') <= 10
    assert '아직 기록이 없어요' in MockWatchStatus(clock=lambda:NOW).validation_summary()
    with pytest.raises(KeyError): view._validation.record('PRIVATE')


def test_telegram_inspection_authorization_replay_and_mode(sandbox):
    service,broker,_,_ = sandbox
    view = MockWatchStatus(clock=lambda:NOW)
    service.watch_validation = view.validation_summary
    service.orders_enabled = False
    bad = update(sandbox,1,'/모의점검')
    bad['message']['from']['id'] = 99
    assert service.handle(bad) is None
    message = update(sandbox,2,'/모의점검')
    first = service.handle(message)
    assert '아직 기록' in first
    view.reset()
    assert service.handle(message) == first
    assert '모의 연결 확인 시작' in service.handle(update(sandbox,3,'/모의점검'))
    service.orders_enabled = True
    assert '점검이 없어요' in service.handle(update(sandbox,4,'/모의점검'))
    assert broker.calls == [] and not view.take_refresh() and not view.take_reconnect()


def test_guided_next_step_handles_day_change_and_clock_reversal_without_actions():
    from datetime import timedelta
    current=[NOW]
    view=MockWatchStatus(clock=lambda:current[0])
    assert '다음 할 일: 집 PC' in view.validation_summary()
    view.reset();view.verified(report())
    assert '모의 알림 연결 상태' in view.validation_summary()
    view.enable_refresh()
    assert '다음 할 일: /잔고' in view.validation_summary()
    view.finish_refresh(report())
    assert '직접 비교하세요' in view.validation_summary()
    current[0]=NOW+timedelta(seconds=31)
    assert '다음 할 일: /잔고' in view.validation_summary()
    current[0]=NOW+timedelta(days=1)
    assert '오늘 재확인 필요' in view.validation_summary()
    current[0]=NOW-timedelta(seconds=1)
    assert '오늘 재확인 필요' in view.validation_summary()
    assert not view.take_refresh() and not view.take_reconnect()
