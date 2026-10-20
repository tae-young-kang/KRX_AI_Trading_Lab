"""MOCK-only, single-attempt HTTP, behind explicitly started watch workers.

Credentials live in memory. Mutations belong behind MockDispatchJournal;
neither this transport nor its journal implements the offline broker protocol.
"""
from datetime import datetime
import re
import threading
import time
import uuid

import requests
from requests.adapters import HTTPAdapter

from app.kiwoom_mock_contract import (
    MockPlan, ContractError, UncertainResponse, _success,
    order_plan, amend_plan, cancel_plan, query_plan, query_page, market_quote_plan,
)
from app.order_engine import OrderRequest
from app.trading_clock import KST


def validated_plan(plan):
    """Reject hand-crafted destinations, API IDs, extra fields and order types."""
    if type(plan) is not MockPlan:
        raise ContractError('모의 요청 형식을 확인하세요.')
    try:
        body = plan.body()
        if plan.api_id in ('kt10000', 'kt10001'):
            canonical = order_plan(OrderRequest(body['stk_cd'],
                'BUY' if plan.api_id == 'kt10000' else 'SELL',
                int(body['ord_qty']), int(body['ord_uv'])))
        elif plan.api_id == 'ka10095':
            canonical = market_quote_plan(body['stk_cd'])
        elif plan.api_id == 'kt10002':
            canonical = amend_plan(body['stk_cd'], body['orig_ord_no'],
                remaining_quantity=int(body['mdfy_qty']), limit_price=int(body['mdfy_uv']))
        elif plan.api_id == 'kt10003':
            canonical = cancel_plan(body['stk_cd'], body['orig_ord_no'],
                remaining_quantity=int(body['cncl_qty']))
        else:
            if plan.api_id == 'kt00009':
                day = datetime.strptime(body['ord_dt'], '%Y%m%d').date()
                canonical = query_plan('executions', trade_date=day)
            else:
                canonical = next(query_plan(kind) for kind in
                    ('unfilled', 'fills', 'holdings', 'deposit', 'identity') if query_plan(kind).api_id == plan.api_id)
        if (len(plan.fields) != len(body) or body != canonical.body()
                or any(type(k) is not str or type(v) is not str for k, v in plan.fields)):
            raise ValueError
    except (KeyError, ValueError, TypeError, StopIteration):
        raise ContractError('지원하지 않는 모의 요청입니다.') from None
    return canonical


class MockTransport:
    environment = 'MOCK'

    def __init__(self, *, environment, profile_id, session=None, clock=None,
                 monotonic=None, pause=None, cancelled=None):
        if environment != 'MOCK':
            raise ContractError('모의 서버 전용 연결입니다.')
        # Opaque local profile identity, never an account number.
        if not isinstance(profile_id, str) or not re.fullmatch(r'[a-f0-9]{32}', profile_id):
            raise ContractError('모의 프로필 식별자가 필요합니다.')
        self.profile_id = profile_id
        self._clock = clock or (lambda: datetime.now(KST))
        self.cancelled = cancelled or (lambda: False)
        self.mutation_allowed = lambda: True  # Explicit watch workers replace this gate.
        self._monotonic = monotonic or time.monotonic
        self._pause = pause or time.sleep
        self._io_lock = threading.Lock()
        self._last_request = None
        self._session = session if session is not None else requests.Session()
        if session is None:
            self._session.trust_env = False
            self._session.mount('https://', HTTPAdapter(max_retries=0))
        self._token = None
        self._expires = None
        self._auth_generation = uuid.uuid4().hex

    def _post(self, path, headers, body):
        try:
            with self._io_lock:
                if self.cancelled():
                    raise ValueError
                if self._last_request is not None:
                    delay = 1.1 - (self._monotonic() - self._last_request)
                    if delay > 0:
                        self._pause(delay)
                self._last_request = self._monotonic()
                if self.cancelled():
                    raise ValueError
                if path.endswith('/ordr') and not self.mutation_allowed():
                    raise ValueError
                if path.endswith('/ordr'):
                    self.ready()
                response = self._session.post('https://mockapi.kiwoom.com' + path,
                    headers=headers, json=body, timeout=(3, 10), allow_redirects=False)
            if response.status_code != 200:
                raise ValueError
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError
            headers = dict(response.headers)
        except Exception:
            # Do not retain URLs, auth headers, body, or broker response text.
            raise UncertainResponse('모의 응답을 확인할 수 없습니다. 자동 재전송하지 않습니다.') from None
        return payload, headers

    def login(self, appkey, secretkey):
        self._auth_generation = uuid.uuid4().hex
        self._token = self._expires = None
        if any(not isinstance(value, str) or not value.strip() for value in (appkey, secretkey)):
            raise ContractError('모의 인증정보를 확인하세요.')
        payload, _ = self._post('/oauth2/token',
            {'Content-Type': 'application/json;charset=UTF-8'},
            {'grant_type': 'client_credentials', 'appkey': appkey, 'secretkey': secretkey})
        _success(payload)
        try:
            token = payload['token']
            if (not isinstance(token, str) or not 1 <= len(token) <= 1000
                    or any(char.isspace() for char in token)
                    or payload['token_type'].lower() != 'bearer'):
                raise ValueError
            expires = datetime.strptime(payload['expires_dt'], '%Y%m%d%H%M%S').replace(tzinfo=KST)
            if expires <= self._clock():
                raise ValueError
        except (KeyError, ValueError, TypeError, AttributeError):
            raise UncertainResponse('모의 인증 응답이 올바르지 않습니다.') from None
        self._token, self._expires = token, expires

    def ready(self):
        if self.cancelled():
            raise ContractError('사용자가 연결을 중지했습니다.')
        if not self._token or self._expires <= self._clock():
            raise ContractError('모의 인증이 필요하거나 만료됐습니다. 주문을 보내지 않았습니다.')

    def exchange(self, plan, *, cursor=None):
        plan = validated_plan(plan)
        self.ready()
        if cursor is not None and (plan.api_id.startswith('kt100') or plan.api_id == 'ka10095'
                or not isinstance(cursor, str) or not cursor or len(cursor) > 50
                or any(ord(char) < 32 for char in cursor)):
            raise ContractError('연속 조회 정보를 확인하세요.')
        headers = {'Content-Type': 'application/json;charset=UTF-8',
            'authorization': 'Bearer ' + self._token, 'api-id': plan.api_id,
            'cont-yn': 'Y' if cursor else 'N', 'next-key': cursor or ''}
        payload, response_headers = self._post(
            plan.url.removeprefix('https://mockapi.kiwoom.com'), headers, plan.body())
        returned_api = {str(k).lower(): v for k, v in response_headers.items()}.get('api-id')
        if returned_api is not None and returned_api != plan.api_id:
            raise UncertainResponse('모의 응답 종류가 일치하지 않습니다.')
        return payload, response_headers

    def read_market_quote(self, code):
        """Read fresh KRX evidence, without authenticating or enabling orders."""
        from app.mock_market_quote import parse_quote
        plan = market_quote_plan(code)
        if self.environment != 'MOCK':
            raise ContractError('모의 전용 시세 연결이 필요해요.')
        self.ready()
        profile, generation = self.profile_id, self.auth_generation
        started = self._clock()
        payload, headers = self.exchange(plan)
        finished = self._clock()
        self.ready()
        if (self.environment != 'MOCK' or self.profile_id != profile
                or self.auth_generation != generation):
            raise ContractError('모의 연결이 바뀌었어요. 시세를 다시 확인해 주세요.')
        return parse_quote(payload, headers, code=code, started_at=started, fetched_at=finished)

    @property
    def auth_generation(self):
        return self._auth_generation

    @property
    def current_day(self):
        moment = self._clock()
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ContractError('한국 날짜를 확인할 수 없습니다.')
        return moment.astimezone(KST).date()

    def read_all(self, kind, *, max_pages=100, trade_date=None):
        if kind not in ('unfilled', 'fills', 'holdings', 'executions') or type(max_pages) is not int or max_pages < 1:
            raise ContractError('조회 범위를 확인하세요.')
        rows, cursor, seen = [], None, set()
        for _ in range(max_pages):
            payload, headers = self.exchange(query_plan(kind, trade_date=trade_date), cursor=cursor)
            page, cursor = query_page(kind, payload, headers)
            rows.extend(page)
            if cursor is None:
                return rows
            if cursor in seen:
                break
            seen.add(cursor)
        raise UncertainResponse('전체 페이지를 확인하지 못했습니다. 잔고 대조를 중단하세요.')

    def close(self):
        self._auth_generation = uuid.uuid4().hex
        self._token = self._expires = None
        self._session.close()
