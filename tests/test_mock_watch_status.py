"""Remote status uses a sanitized memory view, not Qt widgets or broker IO."""
from app.mock_watch_status import MockWatchStatus
from test_telegram_orders import sandbox, update
from test_kiwoom_mock_status import NOW


def test_telegram_status_has_timestamp_and_no_private_report_fields(sandbox):
    service,broker,_,_ = sandbox
    view = MockWatchStatus(clock=lambda:NOW)
    view.status('모의 주문 알림 받는 중 · 주문 불가')
    view.verified(dict(checked_at='10/19 10:00:00',order_money=10000,held=3,pending=1,
                       account='PRIVATE_ACCOUNT',token='PRIVATE_SESSION'))
    service.watch_status,service.orders_enabled = view.summary,False
    reply = service.handle(update(sandbox,1,'/프로그램상태'))
    assert '프로그램이 켜져 있어요' in reply and '10,000원' in reply
    assert 'PRIVATE' not in reply
    assert '주문: 잠금' in reply and broker.calls == []
    view.notice('종목 900001 · 취소 처리 알림이에요.')
    reply = service.handle(update(sandbox,2,'/모의상태'))
    assert '최신 값이 아닐 수' in reply and '종목 900001' in reply
    view.stopped()
    assert '연결 종료' in service.handle(update(sandbox,3,'/상태'))
    unauthorized = update(sandbox,4,'/프로그램상태')
    unauthorized['message']['from']['id'] = 99
    assert service.handle(unauthorized) is None and broker.calls == []


def test_reset_and_status_provider_do_not_create_or_load_credentials():
    view = MockWatchStatus(clock=lambda:NOW)
    assert '확인한 모의 잔고가 없어요' in view.summary()
    view.verified(dict(checked_at='10/19 10:00:00',order_money=None,held=0,pending=2))
    assert '다시 확인 필요' in view.summary()
    view.reset()
    assert '확인한 모의 잔고가 없어요' in view.summary()
