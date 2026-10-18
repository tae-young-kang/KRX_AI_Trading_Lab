"""Authenticated fake updates + temporary settings only; no Telegram or GUI."""
from copy import deepcopy
import re

import pytest

from app.config import DEFAULTS
from app.telegram_conditions import ConditionCommands
from test_telegram_orders import sandbox, update


def code(reply):
    return re.search(r'/조건확인 ([A-Za-z0-9_-]{16})', reply)[1]


def setup(tmp_path):
    cfg = deepcopy(DEFAULTS)
    return cfg, ConditionCommands(tmp_path/'conditions.db', cfg)


def send(controller, text, uid=1, now=1000, session='session', scope='allowed'):
    return controller.handle(text, scope=scope, session=session, uid=uid, now=now)


def test_preview_confirm_apply_once_and_all_market_etf_controls(tmp_path):
    cfg, controller = setup(tmp_path)
    text = '/조건변경 시장=코스닥 ETF=제외 거래대금=200'
    preview = send(controller, text)
    assert send(controller, text) == preview
    assert cfg['market'] == '전체'
    confirm = '/조건확인 '+code(preview)
    assert '예약' in send(controller, confirm, uid=2)
    saved = []
    assert not controller.sync(cfg, lambda:saved.append(1), now=1001, busy=True)
    assert controller.sync(cfg, lambda:saved.append(1), now=1002)
    assert cfg['market'] == 'KOSDAQ' and cfg['include_etf'] is False and cfg['min_value_eok'] == 200
    assert saved == [1]
    assert not controller.sync(cfg, lambda:saved.append(1), now=1003)
    assert '이미 사용' in send(controller, confirm, uid=3)
    assert '최근 변경 적용됨' in send(controller, '/조건', uid=4)


@pytest.mark.parametrize('text', ['/조건변경 data_mode=REAL', '/조건변경 매수=1',
    '/조건변경 거래대금=NaN', '/조건변경 추천수=1.5', '/조건변경 시장=코스피 시장=코스닥',
    '/조건변경 최저가=999999999 최고가=1'])
def test_invalid_or_order_fields_never_queue(tmp_path, text):
    cfg, controller = setup(tmp_path)
    before = dict(cfg)
    assert '/조건확인 ' not in send(controller, text)
    assert not controller.sync(cfg, lambda:None, now=1001)
    assert cfg == before


def test_stale_mode_expiry_owner_and_scope_never_apply(tmp_path):
    cfg, controller = setup(tmp_path)
    token = code(send(controller, '/조건변경 거래대금=200'))
    assert '만료' in send(controller, '/조건확인 '+token, uid=2, now=1120)
    token = code(send(controller, '/조건변경 시장=코스피', uid=3, now=1200))
    assert '만료' in send(controller, '/조건확인 '+token, uid=4, now=1201, scope='other')
    send(controller, '/조건확인 '+token, uid=5, now=1201)
    cfg['data_mode'] = 'KIWOOM'
    cfg['kiwoom_env'] = 'REAL'
    assert not controller.sync(cfg, lambda:None, now=1202)
    assert cfg['market'] == '전체'
    token = code(send(controller, '/조건변경 ETF=제외', uid=6, now=1300))
    send(controller, '/조건확인 '+token, uid=7, now=1301)
    second = ConditionCommands(controller.path, cfg)
    assert not second.sync(cfg, lambda:None, now=1302)  # Another window/restart cannot apply old owner's queue.
    assert cfg['include_etf'] is True


def test_save_failure_restores_memory_and_cancel_session(tmp_path):
    cfg, controller = setup(tmp_path)
    preview = send(controller, '/조건변경 ETF=제외')
    send(controller, '/조건확인 '+code(preview), uid=2)
    def fail():
        raise OSError('fixture failure')
    with pytest.raises(OSError):
        controller.sync(cfg, fail, now=1001)
    assert cfg['include_etf'] is True
    controller.cancel_session('allowed', 'session')
    assert not controller.sync(cfg, lambda:None, now=1002)


def test_authenticated_condition_only_receiver_never_places_orders(sandbox, tmp_path):
    service, broker, _, now = sandbox
    cfg, controller = setup(tmp_path)
    service.conditions, service.orders_enabled = controller, False
    unauthorized = update(sandbox, 1, '/조건')
    unauthorized['message']['from']['id'] = 99
    assert service.handle(unauthorized) is None
    assert '주문은 할 수 없어요' in service.handle(update(sandbox, 2, '/매수 900001 1 1000'))
    preview = service.handle(update(sandbox, 3, '/조건변경 시장=코스피 ETF=제외'))
    service.handle(update(sandbox, 4, '/조건확인 '+code(preview)))
    assert controller.sync(cfg, lambda:None, now=now[0].timestamp()+1)
    assert cfg['market'] == 'KOSPI' and cfg['include_etf'] is False
    assert broker.calls == []
    service.handle(update(sandbox, 5, '/중지'))
    assert '중지' in service.handle(update(sandbox, 6, '/조건변경 ETF=포함'))


def test_stopping_practice_also_discards_pending_condition_change(sandbox, tmp_path):
    service, broker, _, now = sandbox
    cfg, controller = setup(tmp_path)
    service.conditions = controller
    preview = service.handle(update(sandbox, 1, '/조건변경 ETF=제외'))
    service.handle(update(sandbox, 2, '/조건확인 '+code(preview)))
    service.handle(update(sandbox, 3, '/중지'))
    assert not controller.sync(cfg, lambda:None, now=now[0].timestamp()+1)
    assert cfg['include_etf'] is True and broker.calls == []
