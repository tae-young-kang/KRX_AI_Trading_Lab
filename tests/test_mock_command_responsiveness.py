"""Bounded command wait against blocked fake preflight, never real IO."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import pytest

from app.order_engine import connect
from test_mock_telegram_orders import rig, handle, queue, add_capture, mutations
from test_telegram_orders import sandbox, update


@pytest.mark.parametrize('text', ['/주문상태', '/주문목록', '/미체결', '/모의재개',
    '/모의재개확인 unused', '/매수 900001 1 1000', '/매도 900001 1 1000',
    '/정정 13 1 1000', '/취소 13', '/모의확인 unused', '/모의철회 unused'])
def test_busy_command_is_not_queued_or_replayed_later(tmp_path, text):
    cmd, session, http, _, _ = rig(tmp_path)
    entered, release = Event(), Event()
    def owner():
        with cmd.lock:
            entered.set()
            assert release.wait(10)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = pool.submit(owner)
            try:
                assert entered.wait(3)
                before = len(http.calls)
                reply = pool.submit(handle, cmd, text).result(timeout=2)
                assert '이번 명령은 처리하지 않았고 자동 실행하지 않아요' in reply
                assert len(http.calls) == before and mutations(http) == []
                with connect(cmd.path) as db:
                    assert db.execute('SELECT COUNT(*) FROM intents').fetchone()[0] == 0
            finally:
                release.set()
            pending.result(timeout=2)
        assert cmd.process(session) is None
        assert '주문 기록 없음' in handle(cmd, '/주문상태')
    finally:
        cmd.disarm(); session.close()


def test_busy_telegram_command_does_not_block_next_emergency_stop(tmp_path, sandbox, monkeypatch):
    cmd, session, http, _, _ = rig(tmp_path)
    queue(cmd); add_capture(http)
    entered, release = Event(), Event()
    original = session.capture_for_order
    def capture():
        entered.set()
        assert release.wait(10)
        return original()
    monkeypatch.setattr(session, 'capture_for_order', capture)
    service, broker, _, _ = sandbox
    service.orders_enabled = False
    service.watch_orders = cmd.handle
    message = update(sandbox, 1, '/주문상태')
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            dispatch = pool.submit(cmd.process, session)
            try:
                assert entered.wait(3)
                first = pool.submit(service.handle, message).result(timeout=2)
                assert '이번 명령은 처리하지 않았고 자동 실행하지 않아요' in first
                check = pool.submit(service.handle, update(sandbox, 2, '/주문점검')).result(timeout=2)
                assert '처리 중' in check
                stopped = pool.submit(service.handle, update(sandbox, 3, '/긴급중지')).result(timeout=2)
                assert '새 전송을 차단' in stopped and cmd.emergency.event.is_set()
            finally:
                release.set()
            assert dispatch.result(timeout=3) == 'FAILED'
        assert service.handle(message) == first  # Replay never becomes a new action.
        assert mutations(http) == [] and broker.calls == []
        assert '긴급중지' in service.handle(update(sandbox, 4, '/주문점검'))
    finally:
        cmd.disarm(); session.close()
