"""Fixed actionable transport failures; remote text and token URLs never escape."""
import pytest
import requests
from app.telegram_receiver import TelegramTransport,TelegramConnectionError


@pytest.mark.parametrize('status,method,code',[(401,'getMe','AUTH'),(409,'getUpdates','CONFLICT'),
    (403,'sendMessage','FORBIDDEN'),(400,'sendMessage','CHAT'),(400,'getMe','RESPONSE'),
    (429,'getUpdates','RATE_LIMIT'),(503,'getUpdates','SERVER'),(302,'getMe','RESPONSE')])
@pytest.mark.parametrize('http_error',[True,False])
def test_fixed_guidance_ignores_remote_description(status,method,code,http_error):
    calls=[]
    class Response:
        status_code=status if http_error else 200
        def json(self):return {'ok':False,'error_code':status,'description':'PRIVATE_REMOTE_TEXT'}
    class Session:
        def post(self,url,**kwargs):calls.append(1);return Response()
    client=TelegramTransport('12345:'+('x'*30),Session())
    with pytest.raises(TelegramConnectionError) as caught:client._call(method,{})
    assert caught.value.code==code and len(calls)==1
    assert 'PRIVATE_REMOTE_TEXT' not in str(caught.value)
    assert 'https://' not in str(caught.value)


def test_network_exception_has_actionable_fixed_message():
    class Session:
        def post(self,url,**kwargs):raise requests.Timeout(url)
    with pytest.raises(TelegramConnectionError) as caught:
        TelegramTransport('12345:'+('x'*30),Session()).identity()
    assert caught.value.code=='NETWORK' and '인터넷' in str(caught.value)
    assert 'https://' not in str(caught.value)
