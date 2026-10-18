"""Temporary stores and fake broker only; list commands must never perform IO."""
from dataclasses import replace
from datetime import timedelta
import json

import pytest

from app.order_engine import connect
from test_mock_telegram_orders import handle, rig
from test_mock_telegram_actions import ready, action
from test_telegram_orders import sandbox, update
from test_kiwoom_mock_dispatch import accepted
from test_mock_telegram_orders import add_capture


def test_lists_are_scoped_readonly_and_history_survives_disconnect(tmp_path):
    cmd, session, http, _, _, _ = ready(tmp_path)
    before = len(http.calls)
    pending = handle(cmd, '/미체결')
    history = handle(cmd, '/주문목록')
    assert '0000013' in pending and '남은 2주' in pending and '조회 시각' in pending
    assert '900001' in history and '0000013' in history and '마지막 관측' in history
    assert '0000013' not in handle(cmd, '/미체결', scope='other')
    assert '900001' not in handle(cmd, '/주문목록', scope='other')
    assert len(http.calls) == before
    handle(cmd, '/긴급중지')
    assert '0000013' in handle(cmd, '/미체결')
    assert cmd.emergency.event.is_set() and not cmd.armed
    cmd.disarm(); session.close()
    assert '0000013' in handle(cmd, '/주문목록')
    assert '오래됐어요' in handle(cmd, '/미체결')


@pytest.mark.parametrize('change', ['stale', 'future', 'profile', 'auth', 'account', 'day'])
def test_pending_rejects_stale_or_changed_context(tmp_path, change):
    cmd, session, http, _, clock, _ = ready(tmp_path)
    before = len(http.calls)
    cap = cmd._capture
    if change == 'stale': clock[0] += timedelta(seconds=31)
    if change == 'future': clock[0] -= timedelta(seconds=1)
    if change == 'profile': cmd._capture = replace(cap, profile_id='other')
    if change == 'auth': cmd._capture = replace(cap, auth_generation='other')
    if change == 'account': cmd._capture = replace(cap, observations=replace(cap.observations, account_tag='other'))
    if change == 'day': cmd._capture = replace(cap, observations=replace(cap.observations, day=clock[0].date()+timedelta(days=1)))
    reply = handle(cmd, '/미체결')
    assert '0000013' not in reply and '조회' in reply
    assert len(http.calls) == before
    cmd.disarm(); session.close()


def test_pending_hides_superseded_number_after_amend_ack(tmp_path):
    cmd, session, http, _, _, data = ready(tmp_path)
    action(cmd, '/정정 13 2 1100')
    add_capture(http, data); http.responses.append(accepted('0000014', '0000013'))
    assert cmd.process(session) == 'ACKNOWLEDGED'
    before = len(http.calls)
    reply = handle(cmd, '/미체결')
    assert '0000013' not in reply and '대조 필요' in reply
    assert len(http.calls) == before
    cmd.disarm(); session.close()


@pytest.mark.parametrize('kind', ['external', 'quantity', 'empty'])
def test_pending_never_exposes_unmatched_orders_or_claims_completion(tmp_path, kind):
    cmd, session, http, _, _, _ = ready(tmp_path)
    cap = cmd._capture
    rows = {'external': (('0000099', '900001', 3, 2),),
            'quantity': (('0000013', '900001', 9, 2),), 'empty': ()}[kind]
    cmd._capture = replace(cap, observations=replace(cap.observations, unfilled=rows))
    before = len(http.calls)
    reply = handle(cmd, '/미체결')
    assert '0000099' not in reply and '0000013' not in reply
    assert '증명하지는 않아요' in reply and len(http.calls) == before
    cmd.disarm(); session.close()


def test_history_pagination_is_bounded_without_leaking_confirmation(tmp_path):
    cmd, session, http, _, _ = rig(tmp_path)
    payload = json.dumps(dict(code='900001', side='BUY', quantity=1, limit_price=1000))
    with connect(cmd.path) as db:
        for index in range(12):
            db.execute('INSERT INTO intents VALUES (?,?,?,?,?,?,?,?,?)',
                       (str(index), 'owner', 'private-session', index, 'private-hash', payload,
                        cmd.clock().timestamp()+120, 'WAITING', cmd.day.isoformat()))
    before = len(http.calls)
    first = handle(cmd, '/주문목록')
    last = handle(cmd, '/주문목록 3')
    assert '1/3' in first and first.count('900001') == 5 and '/주문목록 2' in first
    assert last.count('900001') == 2 and '3/3' in last
    assert len(first) < 4096 and 'private-' not in first
    assert '3페이지' in handle(cmd, '/주문목록 4')
    for text in ('/주문목록 0', '/미체결 -1', '/주문목록 9999', '/미체결 x'):
        assert '사용법' in handle(cmd, text)
    assert len(http.calls) == before
    with connect(cmd.path) as db:
        assert db.execute("SELECT COUNT(*) FROM intents WHERE state='WAITING'").fetchone()[0] == 12
    cmd.disarm(); session.close()


def test_routing_after_stop_still_authenticates_and_does_not_touch_practice(sandbox):
    service, broker, _, _ = sandbox
    service.orders_enabled = False
    calls = []
    service.watch_orders = lambda text, **context: calls.append((text, context)) or '읽기 전용 목록'
    service.handle(update(sandbox, 1, '/중지'))
    for uid, text in enumerate(('/주문목록 2', '/미체결'), 2):
        event = update(sandbox, uid, text)
        assert service.handle(event) == '읽기 전용 목록'
        assert service.handle(event) == '읽기 전용 목록'
    assert len(calls) == 2 and all(c[1]['scope'] == service.scope for c in calls)
    foreign = update(sandbox, 10, '/미체결')
    foreign['message']['from']['id'] = 99
    service.handle(foreign)
    assert len(calls) == 2 and broker.calls == []
