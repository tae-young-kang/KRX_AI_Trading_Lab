from concurrent.futures import ThreadPoolExecutor
from app.recommendation_request import RecommendationRequest
from test_telegram_orders import sandbox, update
import ast
from pathlib import Path
from types import SimpleNamespace


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
    request.request()
    assert not request.take('REAL_QUOTES', busy=False, active=True)
    assert '모드가 바뀌어' in request.summary()
    request.request()
    assert not request.take('MOCK', busy=False, active=False)
    request.request()
    assert request.take('MOCK', busy=False, active=True)
    request.finish(success=False)
    assert '완료하지 못했어요' in request.summary()


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


def test_gui_handoff_and_saved_result_skip_trade_side_effects():
    bridge = RecommendationRequest('MOCK')
    bridge.request()
    calls = []
    fake = SimpleNamespace(remote_scan=bridge, work=None, cfg={},
                           telegram_order_worker=SimpleNamespace(isRunning=lambda: True,
                               isInterruptionRequested=lambda: False,
                               service=SimpleNamespace(accepts_search_requests=lambda: True)),
                           scan=lambda **kwargs: calls.append(kwargs))
    mode = lambda cfg: SimpleNamespace(key='MOCK')
    gui_method('poll_remote_scan', current_mode=mode)(fake)
    gui_method('poll_remote_scan', current_mode=mode)(fake)
    assert calls == [dict(remote=True)]
    fake.remote_scan_active = True
    fake.db = SimpleNamespace(save_scan=lambda *args: calls.append('saved') or 1)
    fake.history = object()
    fake.fill_stock_tables = lambda: calls.append('table')
    fake.count_info = SimpleNamespace(setText=lambda text: None)
    gui_method('on_scan', current_mode=mode, evaluate=lambda stocks, cfg: [],
               record_nxt_evidence=lambda *args: None)(fake, [], {'market': '전체'}, 'KIWOOM')
    assert calls == [dict(remote=True), 'saved', 'table']
    assert '저장했어요' in bridge.summary() and not fake.remote_scan_active
