import pytest
from app import telegram_menu
from app.telegram_receiver import TelegramTransport
from app.recommendation_request import RecommendationRequest
from test_telegram_orders import sandbox,update


def test_menu_navigation_auth_replay_and_stop(sandbox):
    service,broker,_,_=sandbox
    service.recommendation_request=RecommendationRequest('DEMO')
    event=update(sandbox,1,'/start')
    reply=service.handle(event)
    assert reply==service.handle(event) and reply.startswith(telegram_menu.HEADER)
    assert '가짜 서버로 연습' in reply and len(reply)<700
    assert '검색을 요청했어요' in service.handle(update(sandbox,2,'새 추천 찾기'))
    assert '조회 중' not in service.handle(update(sandbox,3,'검색 진행'))
    service.handle(update(sandbox,4,'/중지'))
    assert '중지 상태' in service.handle(update(sandbox,5,'/메뉴'))
    assert '중지했어요' in service.handle(update(sandbox,6,'새 추천 찾기'))
    assert service.handle(update(sandbox,7,'메뉴 숨기기'))==telegram_menu.HIDDEN
    bad=update(sandbox,8,'/메뉴');bad['message']['from']['id']=99
    assert service.handle(bad) is None and broker.calls==[]


@pytest.mark.parametrize('message,kind',[(telegram_menu.text(practice=False,active=True),'keyboard'),
    (telegram_menu.HIDDEN,'remove_keyboard'),('다른 답장',None)])
def test_reply_keyboard_uses_existing_send_receipt_checks(message,kind):
    sent=[]
    class Response:
        status_code=200
        def json(self): return dict(ok=True,result=dict(message_id=7,chat=dict(id=22)))
    class Session:
        def post(self,url,**kwargs):
            sent.append(kwargs['json'])
            return Response()
    client=TelegramTransport('12345:'+('x'*30),Session())
    assert client.send(22,message)==7
    body=sent[0]
    if kind:
        assert kind in body['reply_markup']
        if kind=='keyboard':
            labels=[button['text'] for row in body['reply_markup']['keyboard'] for button in row]
            assert labels==[label for row in telegram_menu.ROWS for label in row]
            assert all(not command.startswith(('/매수','/매도','/확인')) for command in telegram_menu.COMMANDS.values())
    else:
        assert 'reply_markup' not in body


def test_condition_buttons_preview_confirm_and_preserve_other_settings(sandbox, tmp_path):
    from copy import deepcopy
    import re
    from app.config import DEFAULTS
    from app.telegram_conditions import ConditionCommands
    service, broker, _, now = sandbox
    cfg = deepcopy(DEFAULTS)
    controller = ConditionCommands(tmp_path/'conditions.db', cfg)
    service.conditions, service.orders_enabled = controller, False
    guide = service.handle(update(sandbox, 1, '조건 바꾸기'))
    assert guide.startswith(telegram_menu.CONDITION_HEADER)
    keyboard = telegram_menu.markup(guide)['keyboard']
    assert [b['text'] for row in keyboard for b in row] == [x for row in telegram_menu.CONDITION_ROWS for x in row]
    before = deepcopy(cfg)
    event = update(sandbox, 2, '시장: 코스닥')
    preview = service.handle(event)
    assert preview == service.handle(event)
    assert cfg == before
    token = re.search(r'/조건확인 ([A-Za-z0-9_-]{16})', preview)[1]
    assert '예약' in service.handle(update(sandbox, 3, '/조건확인 '+token))
    assert controller.sync(cfg, lambda:None, now=now[0].timestamp()+1)
    assert cfg['market'] == 'KOSDAQ'
    assert all(cfg[k] == before[k] for k in before if k not in ('market','strategy_version'))
    etf = service.handle(update(sandbox, 4, 'ETF: 제외'))
    assert 'ETF 제외' in etf and cfg['include_etf'] == before['include_etf']
    bad = update(sandbox, 5, '시장: 코스피')
    bad['message']['from']['id'] = 99
    assert service.handle(bad) is None
    service.handle(update(sandbox, 6, '/중지'))
    assert '중지' in service.handle(update(sandbox, 7, 'ETF: 포함'))
    assert service.handle(update(sandbox, 8, '메뉴로 돌아가기')).startswith(telegram_menu.HEADER)
    assert broker.calls == []


def test_theme_conditions_are_visible_without_saved_result_or_orders(sandbox):
    from app.theme_one import RULES_TEXT
    service,broker,_,_=sandbox
    service.orders_enabled=False
    assert service.handle(update(sandbox,1,'테마1 조건'))==RULES_TEXT
    service.handle(update(sandbox,2,'/중지'))
    assert service.handle(update(sandbox,3,'/테마1조건'))==RULES_TEXT
    assert broker.calls==[]


def test_theme_menu_describes_current_collection_and_preserves_old_button():
    guide=telegram_menu.text(practice=False,active=True)
    assert '새 수집은 안 함' not in guide
    assert '요청 날짜 기준' in guide and '키움 연결이면 새 시세를 조회' in guide
    assert '그 외 출처는 저장 자료' in guide
    assert telegram_menu.COMMANDS['테마1 종목 찾기']=='/테마1검색'
    assert telegram_menu.COMMANDS['테마1 새 판정']=='/테마1검색'


def test_theme_buttons_share_authorized_single_request_path(sandbox):
    service,broker,_,_=sandbox
    calls=[]
    class Theme:
        def request(self):
            calls.append('request')
            return '검색 요청 접수'
    service.theme_request=Theme()
    service.orders_enabled=False
    event=update(sandbox,1,'테마1 종목 찾기')
    assert service.handle(event)=='검색 요청 접수'
    assert service.handle(event)=='검색 요청 접수'
    assert calls==['request']
    assert service.handle(update(sandbox,2,'테마1 새 판정'))=='검색 요청 접수'
    assert calls==['request','request']
    bad=update(sandbox,3,'테마1 종목 찾기')
    bad['message']['from']['id']=99
    assert service.handle(bad) is None
    service.handle(update(sandbox,4,'/중지'))
    assert '중지' in service.handle(update(sandbox,5,'테마1 종목 찾기'))
    assert len(calls)==2 and broker.calls==[]


@pytest.mark.parametrize('label,heading',[
    ('전체 도움말','[처음 쓰는 순서]'),('매매 방법','[키움 모의 주문 방법]'),
    ('하루 결산','[하루 손익 보기]'),('가상 추적 방법','[추천 성과 가상 기록]'),
    ('조건 개선 방법','[기록으로 조건 개선]')])
def test_guides_are_private_replayable_read_only_and_available_when_stopped(sandbox,label,heading):
    service,broker,_,_=sandbox;service.orders_enabled=False
    event=update(sandbox,1,label)
    reply=service.handle(event)
    assert reply.startswith(heading) and len(reply)<750 and service.handle(event)==reply
    service.handle(update(sandbox,2,'/중지'))
    assert service.handle(update(sandbox,3,label))==reply
    bad=update(sandbox,4,label);bad['message']['from']['id']=99
    assert service.handle(bad) is None and broker.calls==[]


def test_order_guides_separate_fake_and_mock_and_show_unconnected_first_feed(sandbox):
    service,broker,_,_=sandbox
    assert '가짜 서버 주문 연습' in service.handle(update(sandbox,1,'매매 방법'))
    assert '/확인 코드' in service.handle(update(sandbox,2,'/매매안내'))
    service.orders_enabled=False
    mock=service.handle(update(sandbox,3,'/매매안내'))
    assert '/모의확인 코드' in mock and '실제 시세 모드에서는 주문할 수 없어요' in mock
    assert 'NXT·시초가 예약·테마1 전체 실행은 아직 연결 전' in mock
    virtual=service.handle(update(sandbox,4,'/가상안내'))
    assert '목록 고정만으로 매매 기록이 생기지 않아요' in virtual and broker.calls==[]
