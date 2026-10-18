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
            assert labels==list(telegram_menu.COMMANDS)
            assert all(not command.startswith(('/매수','/매도','/확인')) for command in telegram_menu.COMMANDS.values())
    else:
        assert 'reply_markup' not in body
