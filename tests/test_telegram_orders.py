"""Offline update fixtures, fake exchange and temporary journals. No Telegram IO."""
import re
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from app.order_engine import OrderEngine, connect
from app.simulated_broker import SimulatedBroker
from app.telegram_orders import TelegramOrderService
from app.telegram_receiver import TelegramTransport, receive_once
from app.trading_clock import KST, TradingCalendar


@pytest.fixture
def sandbox(tmp_path):
    now = [datetime(2026, 10, 19, 10, tzinfo=KST)]
    broker = SimulatedBroker(tmp_path/'broker.db', 100000)
    engine = OrderEngine(tmp_path/'orders.db', broker, TradingCalendar({now[0].date()}), lambda:now[0])
    service = TelegramOrderService(tmp_path/'telegram.db', engine, 11, 22, 33,
                                   {'900001':'연습종목', '900002':'두번째'}, lambda:now[0])
    return service, broker, engine, now


def update(sandbox, uid, text):
    return dict(update_id=uid, message=dict(message_id=uid, date=int(sandbox[3][0].timestamp()),
                text=text, chat=dict(id=22, type='private'), **{'from':dict(id=11, is_bot=False)}))


def nonce(reply):
    return re.search(r'/확인 ([A-Za-z0-9_-]+)', reply)[1]


def test_preview_confirm_replay_and_actual_sell_after_fake_fill(sandbox):
    service, broker, engine, _ = sandbox
    request = update(sandbox, 1, '/매수 연습종목 2 1,000')
    preview = service.handle(request)
    assert '가짜 서버' in preview and '2주' in preview and '2,000원' in preview
    assert broker.calls == []
    assert service.handle(request) == preview
    confirmation = update(sandbox, 2, '/확인 '+nonce(preview))
    result = service.handle(confirmation)
    assert '체결 0/2주' in result and broker.calls == ['submit']
    assert service.handle(confirmation) == result
    service.handle(update(sandbox, 3, '/확인 '+nonce(preview)))
    assert broker.calls == ['submit']
    with connect(engine.path) as db:
        order_id = db.execute('SELECT id FROM orders').fetchone()[0]
    broker.fill(order_id, 2, 900)
    engine.reconcile(order_id)
    preview = service.handle(update(sandbox, 4, '연습종목 1주 1100원 매도'))
    assert '매도' in preview
    service.handle(update(sandbox, 5, '/확인 '+nonce(preview)))
    assert broker.calls == ['submit', 'submit']
    assert engine.account()['positions'] == {'900001':2}  # sell accepted != filled


@pytest.mark.parametrize('mutation', ['user','chat','group','bot','forward','edit','old','future','before_start'])
def test_untrusted_messages_never_reply_or_order(sandbox, mutation):
    service, broker, _, now = sandbox
    event = update(sandbox, 1, '/매수 900001 1 1000')
    m = event['message']
    if mutation == 'user': m['from']['id'] = 99
    if mutation == 'chat': m['chat']['id'] = 99
    if mutation == 'group': m['chat']['type'] = 'group'
    if mutation == 'bot': m['from']['is_bot'] = True
    if mutation == 'forward': m['forward_origin'] = {}
    if mutation == 'edit': event['edited_message'] = deepcopy(m)
    if mutation == 'old':
        now[0] += timedelta(seconds=121)
    if mutation == 'future': m['date'] += 6
    if mutation == 'before_start': m['date'] -= 1
    assert service.handle(event) is None and broker.calls == []


def test_expiry_replacement_and_withdrawal(sandbox):
    service, broker, _, now = sandbox
    old = nonce(service.handle(update(sandbox, 1, '/매수 900001 1 1000')))
    fresh = nonce(service.handle(update(sandbox, 2, '/매수 900002 1 2000')))
    assert '유효한' in service.handle(update(sandbox, 3, '/확인 '+old))
    assert '철회' in service.handle(update(sandbox, 4, '/철회 '+fresh))
    assert '유효한' in service.handle(update(sandbox, 5, '/확인 '+fresh))
    token = nonce(service.handle(update(sandbox, 6, '/매수 900001 1 1000')))
    now[0] += timedelta(seconds=120)
    assert '만료' in service.handle(update(sandbox, 7, '/확인 '+token))
    assert broker.calls == []


def test_stop_and_restart_invalidate_pending_and_old_owner(sandbox):
    service, broker, engine, now = sandbox
    token = nonce(service.handle(update(sandbox, 1, '/매수 900001 1 1000')))
    service.handle(update(sandbox, 2, '/중지'))
    assert '중지' in service.handle(update(sandbox, 3, '/확인 '+token))
    new = TelegramOrderService(service.path, engine, 11, 22, 33, service.catalog, lambda:now[0])
    service.stop()  # old owner cannot shut down the new session
    assert service.handle(update(sandbox, 4, '/매수 900001 1 1000')) is None
    assert '유효한' in new.handle(update(sandbox, 5, '/확인 '+token))
    assert '/확인' in new.handle(update(sandbox, 6, '/매수 900001 1 1000'))
    assert broker.calls == []


def test_concurrent_confirmation_only_one_submit(sandbox):
    service, broker, _, _ = sandbox
    token = nonce(service.handle(update(sandbox, 1, '/매수 900001 1 1000')))
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(service.handle, [update(sandbox, 2, '/확인 '+token),update(sandbox, 3, '/확인 '+token)]))
    assert broker.calls == ['submit']


def test_ambiguous_broker_response_is_never_retried(sandbox):
    service, broker, _, _ = sandbox
    token = nonce(service.handle(update(sandbox, 1, '/매수 900001 1 1000')))
    broker.lose_next_response = True
    confirmation = update(sandbox, 2, '/확인 '+token)
    assert '미확인' in service.handle(confirmation)
    service.handle(confirmation)
    service.handle(update(sandbox, 3, '/확인 '+token))
    assert broker.calls == ['submit']


@pytest.mark.parametrize('text', ['/매수 900001 101 1','/매수 900001 1 1000001',
                                  '/매수 900001 -1 1000','/매수 900001 1 1,00',
                                  '/매수 없는종목 1 1000','연습종목 1주 매수'])
def test_bad_orders_do_not_reach_engine(sandbox, text):
    service, broker, _, _ = sandbox
    assert '/확인 ' not in service.handle(update(sandbox, 1, text))
    assert broker.calls == []


def test_receive_replay_after_reply_failure_cannot_resubmit(sandbox):
    service, broker, _, _ = sandbox
    token = nonce(service.handle(update(sandbox, 1, '/매수 900001 1 1000')))
    class Transport:
        def updates(self, offset): return [update(sandbox, 2, '/확인 '+token)]
        def send(self, chat, reply): raise TimeoutError('offline reply failure')
    with pytest.raises(TimeoutError):receive_once(service, Transport())
    assert service.cursor() == 3 and broker.calls == ['submit']
    with pytest.raises(TimeoutError):receive_once(service, Transport())
    assert broker.calls == ['submit']


def test_transport_redacts_token_urls_and_disallows_redirects():
    dummy = '12345:'+('x'*30)  # clearly synthetic, not a bot credential
    class Session:
        def post(self, url, **kwargs):
            assert kwargs['allow_redirects'] is False
            raise RuntimeError(url)
    client = TelegramTransport(dummy, Session())
    with pytest.raises(RuntimeError) as error:client.identity()
    assert dummy not in str(error.value) and 'https://' not in str(error.value)


def test_closed_calendar_sell_without_holdings_and_live_engine_rejected(sandbox):
    service, broker, engine, _ = sandbox
    token = nonce(service.handle(update(sandbox, 1, '/매도 900001 1 1000')))
    assert '접수되지' in service.handle(update(sandbox, 2, '/확인 '+token))
    engine.calendar = TradingCalendar()
    token = nonce(service.handle(update(sandbox, 3, '/매수 900001 1 1000')))
    assert '접수되지' in service.handle(update(sandbox, 4, '/확인 '+token))
    assert broker.calls == []
    broker.environment = 'REAL'
    with pytest.raises(ValueError):TelegramOrderService(service.path, engine, 11, 22, 33, {})


def test_changed_engine_environment_cannot_use_old_confirmation(sandbox):
    service, broker, _, _ = sandbox
    token = nonce(service.handle(update(sandbox, 1, '/매수 900001 1 1000')))
    broker.environment = 'REAL'
    assert '주문하지' in service.handle(update(sandbox, 2, '/확인 '+token))
    assert broker.calls == []


def test_practice_sales_command_is_authorized_read_only_and_replayed(sandbox):
    service,broker,_,_=sandbox
    calls=[]
    service.practice_sales=lambda tid,page: calls.append((tid,page)) or '연습 매도 내역'
    assert service.handle(update(sandbox,1,'/연습내역'))=='연습 매도 내역'
    event=update(sandbox,2,'/연습내역 7 2')
    assert service.handle(event)=='연습 매도 내역'
    assert service.handle(event)=='연습 매도 내역'
    bad=update(sandbox,3,'/연습내역 7')
    bad['message']['from']['id']=99
    assert service.handle(bad) is None
    assert '사용법' in service.handle(update(sandbox,4,'/연습내역 -1'))
    assert calls==[(None,1),(7,2)] and broker.calls==[]
    service.stop()
    assert service.handle(update(sandbox,5,'/연습내역'))=='연습 매도 내역'


def test_theme_read_only_command_auth_replay_stop_and_no_orders(sandbox):
    service,broker,_,_=sandbox
    seen=[]
    service.theme_report=lambda page: seen.append(page) or '테마1 저장 결과'
    event=update(sandbox,1,'/테마1 2')
    assert service.handle(event)=='테마1 저장 결과'
    assert service.handle(event)=='테마1 저장 결과'
    assert seen==[2]
    bad=update(sandbox,2,'/테마1')
    bad['message']['from']['id']=99
    assert service.handle(bad) is None
    assert '사용법' in service.handle(update(sandbox,3,'/테마1 0'))
    service.stop()
    assert service.handle(update(sandbox,4,'/테마1'))=='테마1 저장 결과'
    assert seen==[2,1] and broker.calls==[]


@pytest.mark.parametrize('result',[{'message_id':4,'chat':{'id':22}}, {'message_id':True,'chat':{'id':22}},
                                   {'message_id':4,'chat':{'id':99}},None])
def test_send_receipt_checks_message_and_destination(result):
    class Response:
        status_code=200
        def json(self): return dict(ok=True,result=result)
    class Session:
        def post(self,url,**kwargs): return Response()
    client=TelegramTransport('12345:'+('x'*30),Session())
    if result and type(result['message_id']) is int and result['chat']['id']==22:
        assert client.send(22,'가짜 응답 검사')==4
    else:
        with pytest.raises(RuntimeError): client.send(22,'가짜 응답 검사')


def test_theme_delivery_status_is_scoped_read_only_after_stop(sandbox):
    from app.theme_delivery import destination_scope
    service,broker,_,_=sandbox
    seen=[]
    service.theme_status=lambda destination: seen.append(destination) or '미시도 1개'
    service.stop()
    event=update(sandbox,1,'/테마1알림상태')
    assert service.handle(event)=='미시도 1개'
    assert service.handle(event)=='미시도 1개'
    bad=update(sandbox,2,'/테마1알림상태');bad['message']['chat']['id']=99
    assert service.handle(bad) is None
    assert seen==[destination_scope(33,22)] and broker.calls==[]


def test_theme_budget_is_only_preview_and_parses_explicit_percentages(sandbox):
    service,broker,_,_=sandbox
    seen=[]
    service.theme_budget=lambda total,fee,buffer,page: seen.append((total,fee,buffer,page)) or '예산 미리보기'
    event=update(sandbox,1,'/테마1예산 1000000 0.02 2 2')
    assert service.handle(event)=='예산 미리보기'
    assert service.handle(event)=='예산 미리보기'
    assert seen==[(1000000,2,200,2)]
    assert '사용법' in service.handle(update(sandbox,2,'/테마1예산'))
    assert '사용법' in service.handle(update(sandbox,3,'/테마1예산 1000 0.001 2'))
    service.stop()
    service.handle(update(sandbox,4,'/테마1예산 1000 0 0'))
    assert seen[-1]==(1000,0,0,1) and broker.calls==[]


def test_practice_list_page_command_does_not_become_trade_number(sandbox):
    service,broker,_,_=sandbox;seen=[]
    service.practice_sales=lambda tid,page:seen.append((tid,page)) or '목록'
    assert service.handle(update(sandbox,1,'/연습내역 목록 2'))=='목록'
    assert seen==[(None,2)] and broker.calls==[]
    assert '사용법' in service.handle(update(sandbox,2,'/연습내역 목록 0'))


def test_daily_report_uses_korean_date_and_never_sends_orders(sandbox):
    service,broker,_,_=sandbox;seen=[]
    service.practice_daily=lambda day:seen.append(day) or '연습 결산'
    event=update(sandbox,1,'/연습결산')
    assert service.handle(event)=='연습 결산' and service.handle(event)=='연습 결산'
    service.stop()
    assert service.handle(update(sandbox,2,'/연습결산 2026-10-16'))=='연습 결산'
    assert seen==['2026-10-19','2026-10-16'] and broker.calls==[]


def test_daily_report_page_does_not_send_orders(sandbox):
    service,broker,_,_=sandbox;seen=[]
    service.practice_daily=lambda day,page=1:seen.append((day,page)) or '결산 페이지'
    assert service.handle(update(sandbox,1,'/연습결산 2026-10-19 2'))=='결산 페이지'
    assert seen==[('2026-10-19',2)] and broker.calls==[]
