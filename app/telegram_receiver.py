"""Explicit-start Telegram transport; never calls a brokerage HTTP endpoint.

Bot API reference: https://core.telegram.org/bots/api#getupdates
No automatic retries or webhook removal. Errors never contain a token URL.
"""
import re
import requests
from app.telegram_menu import markup


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
                raise ValueError()
            data = response.json()
            if not isinstance(data, dict) or data.get('ok') is not True:
                raise ValueError()
            return data['result']
        except Exception:
            raise RuntimeError('텔레그램 통신이 실패했습니다. 연결 키·네트워크·다른 수신 프로그램을 확인하세요. 자동 재시도하지 않습니다.') from None

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
