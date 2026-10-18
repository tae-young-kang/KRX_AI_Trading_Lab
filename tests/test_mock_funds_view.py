"""Money display uses fake captures only; no broker or Telegram network."""
from datetime import timedelta
from copy import deepcopy
import pytest

from app.mock_watch_status import MockWatchStatus
from test_kiwoom_mock_watch import setup
from test_kiwoom_mock_review import ACCOUNT, wire
from test_kiwoom_mock_status import NOW
from test_telegram_orders import sandbox, update


def verified_view(data=None):
    session, client, http, _, _ = setup(data)
    clock = [client._clock()]
    report = session.verify(ACCOUNT)
    view = MockWatchStatus(clock=lambda:clock[0])
    view.verified(report)
    return view, session, http, clock, report


def test_funds_do_not_add_future_money_or_expose_account():
    view, session, http, _, report = verified_view()
    before = len(http.calls)
    text = view.funds_summary()
    assert '97,010원' in text and 'D+1' in text and 'D+2' in text
    assert ACCOUNT not in text and '또 빼지 않아요' in text and '주문 허용이 아니에요' in text
    assert len(http.calls) == before and report['ready_for_orders'] is False
    # Copy only the numeric allowlist, never retain the original mutable mapping.
    report['funds']['order_money'] = 1
    assert '97,010원' in view.funds_summary()
    session.close()


def test_unknown_order_keeps_available_and_reserved_money_unknown():
    data = wire()
    data[-1]['oso'] = [dict(ord_no='12', orig_ord_no='0', stk_cd='900001', ord_qty='3', oso_qty='2')]
    view, session, _, _, _ = verified_view(data)
    text = view.funds_summary()
    assert text.count('대조 필요') == 2
    assert '지금 주문에 쓸 수 있는 돈(조회값): 대조 필요' in text
    assert '예수금(조회값)' in text
    session.close()


@pytest.mark.parametrize('change', ['age','future','date','notice','order','stop','reset','refresh'])
def test_stale_money_never_looks_current(change):
    view, session, _, clock, _ = verified_view()
    if change == 'age': clock[0] += timedelta(seconds=31)
    elif change == 'future': clock[0] -= timedelta(seconds=1)
    elif change == 'date': clock[0] += timedelta(days=1)
    elif change == 'notice': view.notice('PRIVATE')
    elif change == 'order': view.order_result('ACKNOWLEDGED')
    elif change == 'stop': view.stopped()
    elif change == 'reset': view.reset()
    elif change == 'refresh': view.enable_refresh(); view.request_refresh()
    assert '97,010원' not in view.funds_summary() and '/잔고' in view.funds_summary()
    session.close()


@pytest.mark.parametrize('damage', ['missing','bool','string','extra','timestamp'])
def test_incomplete_or_untrusted_fields_are_never_echoed(damage):
    view, session, _, _, original = verified_view()
    report = deepcopy(original)
    if damage == 'missing': del report['funds']['deposit']
    elif damage == 'bool': report['funds']['deposit'] = True
    elif damage == 'string': report['funds']['order_money'] = 'PRIVATE'
    elif damage == 'extra': report['funds']['account'] = 'PRIVATE'
    elif damage == 'timestamp': report['captured_at'] = NOW.replace(tzinfo=None)
    view.verified(report)
    text = view.funds_summary()
    assert 'PRIVATE' not in text and '상세 조회가 없어요' in text
    session.close()


def test_funds_authentication_mode_and_stopped_read_only_access(sandbox):
    service, broker, _, _ = sandbox
    view, session, http, _, _ = verified_view()
    service.orders_enabled = False
    service.watch_funds = view.funds_summary
    bad = update(sandbox, 1, '/모의자금'); bad['message']['from']['id'] = 99
    assert service.handle(bad) is None
    before = len(http.calls)
    service.handle(update(sandbox, 2, '/중지'))
    assert '97,010원' in service.handle(update(sandbox, 3, '/모의자금'))
    service.orders_enabled = True
    assert '모드를 확인' in service.handle(update(sandbox, 4, '/모의자금'))
    assert broker.calls == [] and len(http.calls) == before
    session.close()
