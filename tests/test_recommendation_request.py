from concurrent.futures import ThreadPoolExecutor
from app.recommendation_request import RecommendationRequest
from test_telegram_orders import sandbox, update
import ast
from pathlib import Path
from types import SimpleNamespace
import pytest


def gui_method(name, **namespace):
    # Compile only a method: never import/construct the GUI or a market client.
    tree = ast.parse((Path(__file__).resolve().parents[1]/'app/gui.py').read_text(encoding='utf-8'))
    window = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'MainWindow')
    method = next(n for n in window.body if isinstance(n, ast.FunctionDef) and n.name == name)
    exec(compile(ast.Module(body=[method], type_ignores=[]), '<offline GUI method>', 'exec'), namespace)
    return namespace[name]


def test_single_claim_busy_mode_and_completion():
    request = RecommendationRequest('MOCK')
    request.request()
    assert not request.take('MOCK', busy=True, active=True)
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda _: request.take('MOCK', busy=False, active=True), range(4)))
    assert sum(claims) == 1
    assert '이미 검색' in request.request()
    request.finish(success=True)
    assert '저장했어요' in request.summary()
    request.send_notice(lambda text: 1, active=True)
    request.request()
    assert not request.take('REAL_QUOTES', busy=False, active=True)
    assert '모드가 바뀌어' in request.summary()
    request.request()
    assert not request.take('MOCK', busy=False, active=False)
    request.request()
    assert request.take('MOCK', busy=False, active=True)
    request.finish(success=False)
    assert '완료하지 못했어요' in request.summary()


@pytest.mark.parametrize('elapsed,starts', [(119.9, True), (120, False), (121, False), (-1, False)])
def test_pending_request_expiry_uses_elapsed_time(elapsed, starts):
    clock = [1000.0]
    request = RecommendationRequest('MOCK', clock=lambda: clock[0])
    request.request()
    clock[0] += elapsed
    assert request.take('MOCK', busy=False, active=True) is starts
    if not starts:
        assert '만료' in request.summary()
        request.request()
        assert request.take('MOCK', busy=False, active=True)


def test_status_expires_busy_request_and_running_progress_survives_wait():
    clock = [0]
    request = RecommendationRequest('MOCK', clock=lambda: clock[0])
    request.request()
    assert not request.take('MOCK', busy=True, active=True)
    clock[0] = 120
    assert '만료' in request.summary()
    request.request()
    assert request.take('MOCK', busy=False, active=True)
    clock[0] = 999
    request.progress('KOSPI 조회 100/900\n'+'x'*300)
    assert 'KOSPI 조회 100/900' in request.summary()
    assert '만료' not in request.summary() and len(request.summary()) < 400
    request.finish(success=True)
    request.progress('late')
    assert 'late' not in request.summary()


def test_cancel_pending_and_running_before_save():
    bridge = RecommendationRequest('MOCK')
    bridge.request()
    assert '취소했어요' in bridge.cancel()
    assert not bridge.take('MOCK', busy=False, active=True)
    bridge.request()
    assert bridge.take('MOCK', busy=False, active=True)
    assert '취소 요청' in bridge.cancel()
    bridge.progress('late progress')
    assert 'late progress' not in bridge.summary()
    fake = SimpleNamespace(remote_scan_active=True, remote_scan=bridge)
    with pytest.raises(RuntimeError, match='취소'):
        gui_method('on_scan')(fake, [], {}, 'KIWOOM')
    bridge.finish(success=False)
    assert '중단 처리를 마쳤어요' in bridge.summary()
    assert not bridge.cancellation_requested()
    assert '취소할 검색 요청이 없어요' in bridge.cancel()


def test_remote_cancel_interrupts_only_remote_work():
    bridge = RecommendationRequest('DEMO')
    bridge.request()
    bridge.take('DEMO', busy=False, active=True)
    bridge.cancel()
    calls = []
    fake = SimpleNamespace(remote_scan=bridge, remote_scan_active=True,
                           telegram_order_worker=None,
                           work=SimpleNamespace(isRunning=lambda: True,
                                                requestInterruption=lambda: calls.append('cancel')))
    gui_method('poll_remote_scan')(fake)
    assert calls == ['cancel']


def test_commands_authorization_replay_stop_and_no_orders(sandbox):
    service, broker, _, _ = sandbox
    request = RecommendationRequest('DEMO')
    service.recommendation_request = request
    bad = update(sandbox, 1, '/추천검색')
    bad['message']['from']['id'] = 99
    assert service.handle(bad) is None
    assert not request.take('DEMO', busy=False, active=True)
    event = update(sandbox, 2, '/추천검색')
    reply = service.handle(event)
    assert '요청했어요' in reply
    assert request.take('DEMO', busy=False, active=True)
    request.finish(success=True)
    assert service.handle(event) == reply
    assert not request.take('DEMO', busy=False, active=True)
    assert '저장했어요' in service.handle(update(sandbox, 3, '/검색상태'))
    service.handle(update(sandbox, 4, '/중지'))
    assert not service.accepts_search_requests()
    assert '중지했어요' in service.handle(update(sandbox, 5, '/추천검색'))
    assert broker.calls == []


def test_cancel_command_after_stop_is_read_only_and_replay_safe(sandbox):
    service, broker, _, _ = sandbox
    bridge = RecommendationRequest('DEMO')
    service.recommendation_request = bridge
    service.handle(update(sandbox, 1, '/추천검색'))
    bridge.take('DEMO', busy=False, active=True)
    service.handle(update(sandbox, 2, '/중지'))
    event = update(sandbox, 3, '/검색취소')
    assert service.handle(event) == service.handle(event)
    assert bridge.cancellation_requested() and broker.calls == []


def test_exact_search_command_pages_replay_and_stop(sandbox):
    service, broker, _, _ = sandbox
    calls = []
    def report(performance, *, scan_id, page):
        calls.append((scan_id, page))
        return f'검색 #{scan_id} 페이지 {page}'
    service.recommendation_report = report
    event = update(sandbox, 1, '/추천결과 123 2')
    assert service.handle(event) == service.handle(event) == '검색 #123 페이지 2'
    assert calls == [(123, 2)]
    service.handle(update(sandbox, 2, '/중지'))
    assert '검색 #123' in service.handle(update(sandbox, 3, '/추천결과 123'))
    for uid, text in enumerate(('/추천결과', '/추천결과 0', '/추천결과 1 0', '/추천결과 x'), 4):
        assert '사용법' in service.handle(update(sandbox, uid, text))
    assert calls == [(123, 2), (123, 1)] and broker.calls == []


def test_gui_handoff_and_saved_result_skip_trade_side_effects():
    bridge = RecommendationRequest('MOCK')
    bridge.request()
    calls = []
    fake = SimpleNamespace(remote_scan=bridge, remote_scan_active=False, remote_theme=None, work=None, cfg={},
                           telegram_order_worker=SimpleNamespace(isRunning=lambda: True,
                               isInterruptionRequested=lambda: False,
                               service=SimpleNamespace(accepts_search_requests=lambda: True)),
                           scan=lambda **kwargs: calls.append(kwargs))
    mode = lambda cfg: SimpleNamespace(key='MOCK')
    gui_method('poll_remote_scan', current_mode=mode)(fake)
    gui_method('poll_remote_scan', current_mode=mode)(fake)
    assert calls == [dict(remote=True)]
    fake.remote_scan_active = True
    fake.db = SimpleNamespace(save_scan=lambda *args,**kwargs: calls.append('saved') or 1,
                              recommendation_search_result=lambda mode, scan_id: '검색 #1 고정 결과')
    fake.history = object()
    fake.fill_stock_tables = lambda: calls.append('table')
    fake.count_info = SimpleNamespace(setText=lambda text: None)
    gui_method('on_scan', current_mode=mode, evaluate=lambda stocks, cfg: [],
               record_nxt_evidence=lambda *args: None)(fake, [], {'market': '전체'}, 'KIWOOM')
    assert calls == [dict(remote=True), 'saved', 'table']
    assert '저장했어요' in bridge.summary() and not fake.remote_scan_active
    notices = []
    bridge.send_notice(lambda text: notices.append(text) or 1, active=True)
    assert '검색 #1 고정 결과' in notices[0]


@pytest.mark.parametrize('receipt', [None, True, 0, -1, '1', 7])
def test_completion_notice_is_claimed_once_and_receipt_is_checked(receipt):
    request = RecommendationRequest('DEMO')
    request.request()
    request.take('DEMO', busy=False, active=True)
    request.finish(success=True)
    calls = []
    def sender(text):
        calls.append(text)
        return receipt
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: request.send_notice(sender, active=True), range(4)))
    assert len(calls) == 1 and '검색 결과를 저장했어요' in calls[0]
    assert ('발송 응답 확인' in request.summary()) is (type(receipt) is int and receipt > 0)


def test_completion_failure_stop_and_old_receipt_do_not_change_new_request():
    request = RecommendationRequest('MOCK')
    request.request()
    request.take('MOCK', busy=False, active=True)
    request.finish(success=False)
    def fails(text):
        raise OSError('synthetic transport error')
    request.send_notice(fails, active=True)
    assert '응답 미확인' in request.summary()
    request.send_notice(lambda text: pytest.fail('must not retry'), active=True)
    request.request()
    request.take('MOCK', busy=False, active=True)
    request.finish(success=True)
    request.send_notice(lambda text: pytest.fail('stopped'), active=False)
    assert '생략' in request.summary()
    request.request()
    request.take('MOCK', busy=False, active=True)
    request.finish(success=True)
    def next_request(text):
        request.request()
        return 9
    request.send_notice(next_request, active=True)
    assert '요청했어요' in request.summary() and '발송 응답 확인' not in request.summary()
