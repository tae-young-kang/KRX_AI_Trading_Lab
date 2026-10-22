"""Explicit-start Telegram transport; never calls a brokerage HTTP endpoint.

Bot API reference: https://core.telegram.org/bots/api#getupdates
No automatic retries or webhook removal. Errors never contain a token URL.
"""
import re
import requests
from app.telegram_menu import markup

ERROR_GUIDANCE={
    'AUTH':'봇 연결 키가 거절됐어요. BotFather에서 받은 Bot Token을 연결 설정에 다시 저장한 뒤 시작해 주세요.',
    'CONFLICT':'다른 프로그램의 봇 수신 또는 웹훅과 충돌했어요. 같은 봇을 쓰는 다른 수신을 중지한 뒤 시작해 주세요. 웹훅을 자동 삭제하지 않았어요.',
    'FORBIDDEN':'봇의 접근이 거절됐어요. 봇 차단을 풀고 본인이 만든 봇 대화방에서 시작을 누른 뒤 Chat ID를 확인해 주세요.',
    'CHAT':'메시지를 보낼 수 없어요. 본인이 만든 봇 대화방에서 시작을 누르고 개인 Chat ID를 확인해 주세요.',
    'RATE_LIMIT':'텔레그램 요청이 너무 많아요. 잠시 기다렸다가 직접 다시 시작해 주세요.',
    'SERVER':'텔레그램 서버 응답에 문제가 있어요. 잠시 뒤 직접 다시 시작해 주세요.',
    'NETWORK':'텔레그램에 연결하지 못했어요. 인터넷·방화벽·프록시를 확인한 뒤 시작해 주세요.',
    'RESPONSE':'텔레그램 응답을 확인하지 못했어요. 연결 설정을 확인한 뒤 시작해 주세요.',
}


class TelegramConnectionError(RuntimeError):
    """Only fixed local guidance, never remote descriptions, URLs or credentials."""
    def __init__(self,code):
        self.code=code if code in ERROR_GUIDANCE else 'RESPONSE'
        super().__init__(ERROR_GUIDANCE[self.code]+' 자동 재시도하지 않습니다.')


def response_error(status,method):
    if status==401:return 'AUTH'
    if status==409:return 'CONFLICT'
    if status==403:return 'FORBIDDEN'
    if status==400 and method=='sendMessage':return 'CHAT'
    if status==429:return 'RATE_LIMIT'
    if type(status) is int and 500<=status<=599:return 'SERVER'
    return 'RESPONSE'


class TelegramTransport:
    def __init__(self, token, session=None):
        if not isinstance(token, str) or not re.fullmatch(r'[0-9]{5,16}:[A-Za-z0-9_-]{20,256}', token):
            raise ValueError('저장한 텔레그램 봇 연결 키를 확인하세요.')
        self._root = 'https://api.telegram.org/bot'+token+'/'
        self.session = session or requests.Session()

    def _call(self, method, body):
        if method not in ('getMe', 'getUpdates', 'sendMessage'):
            raise ValueError('지원하지 않는 텔레그램 요청입니다.')
        try:
            response = self.session.post(self._root+method, json=body, timeout=(3, 8), allow_redirects=False)
            if response.status_code != 200:
                raise TelegramConnectionError(response_error(response.status_code,method))
            data = response.json()
            if not isinstance(data, dict) or data.get('ok') is not True:
                status=data.get('error_code') if isinstance(data,dict) else None
                raise TelegramConnectionError(response_error(status,method))
            return data['result']
        except TelegramConnectionError:
            raise
        except requests.RequestException:
            raise TelegramConnectionError('NETWORK') from None
        except Exception:
            raise TelegramConnectionError('RESPONSE') from None

    def identity(self):
        result = self._call('getMe', {})
        if not isinstance(result, dict) or type(result.get('id')) is not int or result.get('is_bot') is not True:
            raise RuntimeError('텔레그램 봇 정보를 확인하지 못했습니다.')
        return result['id']

    def updates(self, offset):
        body = dict(timeout=1, limit=100, allowed_updates=['message'])
        if offset is not None:
            body['offset'] = offset
        result = self._call('getUpdates', body)
        if not isinstance(result, list) or any(not isinstance(x, dict) or type(x.get('update_id')) is not int or x['update_id'] < 0 for x in result):
            raise RuntimeError('텔레그램 수신 형식을 확인하지 못했습니다.')
        return sorted(result, key=lambda x:x['update_id'])

    def send(self, chat_id, text):
        if type(chat_id) is not int or chat_id==0:
            raise ValueError('텔레그램 대화방을 확인해 주세요.')
        body=dict(chat_id=chat_id,text=text)
        keyboard=markup(text)
        if keyboard is not None: body['reply_markup']=keyboard
        result=self._call('sendMessage', body)
        if (not isinstance(result,dict) or type(result.get('message_id')) is not int or result['message_id']<=0
                or not isinstance(result.get('chat'),dict) or type(result['chat'].get('id')) is not int
                or result['chat']['id']!=chat_id):
            raise RuntimeError('텔레그램 발송 결과의 메시지 번호·대화방을 확인하지 못했습니다. 자동 재시도하지 않습니다.')
        return result['message_id']

    def close(self):
        self.session.close()


def receive_once(service, transport, stopped=lambda:False):
    """Journal/advance cursor before replying; replay can never resubmit orders."""
    for update in transport.updates(service.cursor()):
        if stopped():
            return
        reply = service.handle(update)
        service.cursor(update['update_id']+1)
        if reply is not None and not stopped():
            transport.send(service.chat_id, reply)
