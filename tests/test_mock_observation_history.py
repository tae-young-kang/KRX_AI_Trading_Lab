"""Private temporary diagnostic stores; no GUI, network or executions."""
import sqlite3

import pytest

from app.mock_observation_history import MockObservationHistory
from app.mock_watch_status import MockWatchStatus
from test_mock_validation import report
from test_kiwoom_mock_status import NOW
from test_telegram_orders import sandbox, update


def test_restart_preserves_safe_history_without_restoring_current_state(tmp_path):
    path = tmp_path/'mock'/'observations.db'
    view = MockWatchStatus(clock=lambda:NOW, history_path=path)
    assert not path.exists()
    assert '아직 저장된' in view.history_summary() and not path.exists()
    view.reset(); view.verified(report()); view.enable_refresh()
    view.notice('PRIVATE_RAW_PAYLOAD'); view.finish_refresh(report())
    view.order_result('ACKNOWLEDGED'); view.order_result('UNKNOWN'); view.stopped()
    text = view.history_summary()
    assert '현재 실행' in text and '2026-10-19' in text and '접수 응답 확인' in text
    assert 'PRIVATE' not in text and '100원' not in text and '증명이 아니에요' in text
    restored = MockWatchStatus(clock=lambda:NOW, history_path=path)
    assert '이전 실행' in restored.history_summary()
    assert '아직 기록이 없어요' in restored.validation_summary()
    assert '아직 확인한 모의 잔고가 없어요' in restored.summary()
    assert not restored.take_refresh() and not restored.take_reconnect() and restored._orders is None
    with sqlite3.connect(path) as db:
        columns = [r[1] for r in db.execute('PRAGMA table_info(mock_observations)')]
        assert columns == ['id', 'run', 'stamp', 'event']
        assert all('PRIVATE' not in str(r) for r in db.execute('SELECT * FROM mock_observations'))


def test_history_is_bounded_and_rejects_unknown_events(tmp_path):
    log = MockObservationHistory(tmp_path/'observations.db')
    log.LIMIT = 12
    for _ in range(22): log.record('NOTICE', NOW)
    with sqlite3.connect(log.path) as db:
        assert db.execute('SELECT COUNT(*) FROM mock_observations').fetchone()[0] == 12
    assert log.summary().count('주문 처리 알림 관측') == 10
    assert len(log.summary()) < 4096
    with pytest.raises(KeyError): log.record('PRIVATE', NOW)


@pytest.mark.parametrize('damage', ['file', 'event', 'stamp'])
def test_unreadable_or_tampered_history_reports_safely(tmp_path, damage):
    path = tmp_path/'observations.db'
    log = MockObservationHistory(path)
    log.record('START', NOW)
    if damage == 'file': path.write_text('PRIVATE_CORRUPTION', encoding='utf8')
    else:
        with sqlite3.connect(path) as db:
            db.execute(f'UPDATE mock_observations SET {damage}=?', ('PRIVATE_CORRUPTION',))
    text = log.summary()
    assert '읽을 수 없어요' in text and 'PRIVATE' not in text


def test_storage_failure_is_visible_but_does_not_break_memory_status(tmp_path):
    blocked = tmp_path/'file'
    blocked.write_text('blocked', encoding='utf8')
    view = MockWatchStatus(clock=lambda:NOW, history_path=blocked/'observations.db')
    view.reset(); view.verified(report())
    assert '누락될 수 있어요' in view.history_summary()
    assert '계좌·잔고 조회: 관측됨' in view.validation_summary()
    assert not view.take_refresh()


def test_locked_diagnostic_store_does_not_interrupt_current_status(tmp_path):
    path = tmp_path/'observations.db'
    view = MockWatchStatus(clock=lambda:NOW, history_path=path)
    view.reset()
    with sqlite3.connect(path) as locked:
        locked.execute('BEGIN IMMEDIATE')
        view.notice('PRIVATE')
        assert view._history.failed
        assert '주문 처리 알림 1회' in view.validation_summary()
    assert '누락될 수 있어요' in view.history_summary()


def test_telegram_history_auth_replay_stop_and_practice_separation(tmp_path, sandbox):
    service, broker, _, _ = sandbox
    view = MockWatchStatus(clock=lambda:NOW, history_path=tmp_path/'observations.db')
    service.orders_enabled = False
    service.watch_history = view.history_summary
    view.reset()
    bad = update(sandbox, 1, '/모의기록'); bad['message']['from']['id'] = 99
    assert service.handle(bad) is None
    event = update(sandbox, 2, '/모의기록')
    first = service.handle(event)
    assert '현재 실행' in first
    view.stopped()
    assert service.handle(event) == first
    service.handle(update(sandbox, 3, '/중지'))
    assert '모의 연결 종료' in service.handle(update(sandbox, 4, '/모의기록'))
    service.orders_enabled = True
    assert '볼 수 없어요' in service.handle(update(sandbox, 5, '/모의기록'))
    assert broker.calls == [] and not view.take_refresh() and not view.take_reconnect()


def test_order_outcome_and_warning_never_store_arbitrary_text(tmp_path):
    view = MockWatchStatus(clock=lambda:NOW, history_path=tmp_path/'observations.db')
    for state in ('ACKNOWLEDGED', 'REJECTED', 'UNKNOWN', 'FAILED', 'PRIVATE_BROKER_REPLY'):
        view.order_result(state)
    view.order_warning()
    class Orders:
        def handle(self, text, **context): return '중지 요청 기록'
    view.bind_orders(Orders())
    view.handle_orders('/긴급중지')
    text = view.history_summary()
    assert '긴급중지 요청' in text and '기록 충돌로 주문 차단' in text
    assert 'PRIVATE' not in text
