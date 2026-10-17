"""Market data sources. No account order methods intentionally exist in v0.2."""
import math
import random
import time
from datetime import datetime
from dataclasses import dataclass, asdict
import requests


@dataclass
class Stock:
    code: str
    name: str
    market: str
    price: float
    change_pct: float
    trade_value_eok: float
    vol_ratio_pct: float | None
    cap_eok: float | None
    volume: int = 0
    source: str = 'DEMO'
    stamp: str = ''
    quote_stamp: str = ''
    is_etf: bool = False

    def as_dict(self):
        return asdict(self)


def numeric(obj, default=None, abs_value=False):
    try:
        raw = str(obj).replace(',', '').strip()
        if not raw:
            return default
        value = float(raw)
        if not math.isfinite(value):
            return default
        return abs(value) if abs_value else value
    except (ValueError, TypeError):
        return default


class DemoMarket:
    """Synthetic data for UI and paper order testing. NEVER genuine quotes."""
    BASE = [
        ('900001', '데모 반도체A', 'KOSPI', 74500, 420000, 230),
        ('900002', '데모 자동차B', 'KOSPI', 138000, 180000, 173),
        ('900003', '데모 바이오C', 'KOSDAQ', 32000, 21500, 295),
        ('900004', '데모 소프트웨어D', 'KOSDAQ', 14500, 4400, 260),
        ('900005', '데모 화학E', 'KOSPI', 28000, 12500, 138),
        ('900006', '데모 소재F', 'KOSDAQ', 6200, 6800, 330),
        ('900007', '데모 은행G', 'KOSPI', 56000, 97000, 152),
        ('900008', '데모 통신H', 'KOSPI', 39100, 120000, 180),
        ('900009', '데모 장비I', 'KOSDAQ', 83000, 12800, 141),
        ('900010', '데모 조선J', 'KOSPI', 187000, 105000, 212),
        ('900011', '데모 로봇K', 'KOSDAQ', 27000, 8400, 380),
        ('900012', '데모 유통L', 'KOSPI', 10500, 5100, 125),
    ]
    def __init__(self):
        self.step = 0
        self.random = random.Random(1012)
        self.prices = {item[0]: float(item[3]) for item in self.BASE}

    def scan(self, market='전체'):
        self.step += 1
        result = []
        for i, (code, name, mrkt, _, cap, vr) in enumerate(self.BASE):
            price = self.prices[code] * (1 + self.random.uniform(-0.008, 0.008))
            self.prices[code] = round(price)
            change = (3.8 * math.sin(self.step / 7 + i) + self.random.uniform(-1.0, 1.0))
            value = 65 + 39 * (i % 5) + (self.step % 8) * 3
            if market == '전체' or market == mrkt:
                result.append(Stock(code, name, mrkt, max(1, round(price)), round(change, 2),
                                    round(value, 1), vr + self.random.randint(-22, 22), cap,
                                    110000 + (i * 39541) + self.step * 87, 'DEMO', datetime.now().isoformat(timespec='seconds')))
        return result


class KiwoomAPIError(RuntimeError):
    """A rejected request, without broker response contents or credentials."""


class KiwoomMarket:
    """Read-only REST screener. KRX quotes only for consistent early-stage data tests."""
    QUOTE_BATCH_SIZE = 100
    def __init__(self, appkey, secretkey, env='MOCK'):
        if not appkey or not secretkey:
            raise ValueError('키움 App Key / Secret을 설정해주세요.')
        self.url = 'https://mockapi.kiwoom.com' if env == 'MOCK' else 'https://api.kiwoom.com'
        self.key = appkey
        self.secret = secretkey
        self.token = None
        self.token_ts = 0
        self.last_api_call = 0
        self.session = requests.Session()

    def login(self):
        resp = self.session.post(self.url + '/oauth2/token', json={
            'grant_type': 'client_credentials', 'appkey': self.key, 'secretkey': self.secret
        }, timeout=12)
        resp.raise_for_status()
        data = resp.json()
        if data.get('return_code', data.get('returnCode', 0)) not in (0, '0', None) or not data.get('token'):
            raise RuntimeError('키움 인증 실패: ' + str(data.get('return_msg', '토큰 없음')))
        self.token = data['token']
        self.token_ts = time.time()
        return True

    def query(self, api_id, path, body, cont_yn='N', next_key='',
              include_headers=False, cancelled=None):
        """Retry read-only requests; never expose credentials in request errors."""
        if not self.token or time.time() - self.token_ts > 20 * 3600:
            self.login()
        for attempt in range(3):
            self._check_cancelled(cancelled)
            delay = 1.1 if 'mockapi' in self.url else 0.26
            interval = time.monotonic() - self.last_api_call
            if interval < delay:
                time.sleep(delay - interval)
            self.last_api_call = time.monotonic()
            try:
                resp = self.session.post(self.url + path, headers={
                    'authorization': f'Bearer {self.token}', 'api-id': api_id,
                    'Content-Type': 'application/json;charset=UTF-8',
                    'cont-yn': cont_yn, 'next-key': next_key
                }, json=body, timeout=12)
                if resp.status_code == 429 or resp.status_code >= 500:
                    if attempt < 2:
                        time.sleep(2 ** attempt)
                        continue
                resp.raise_for_status()
                data = resp.json()
            except (requests.Timeout, requests.ConnectionError):
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError(f'{api_id} 조회 연결 실패 (3회 시도)') from None
            except (requests.RequestException, ValueError):
                raise RuntimeError(f'{api_id} 조회 응답 오류') from None
            if not isinstance(data, dict):
                raise RuntimeError(f'{api_id} 응답 형식 오류')
            rc = data.get('return_code', data.get('returnCode'))
            if str(rc) != '0':
                raise KiwoomAPIError(f'{api_id} 조회 실패: 전체 종목 조회를 완료하지 못했습니다.')
            return (data, resp.headers) if include_headers else data

    @staticmethod
    def _check_cancelled(cancelled):
        if cancelled and cancelled():
            raise RuntimeError('전체 종목 조회를 취소했습니다. 미완료 결과는 추천에 사용하지 않습니다.')

    def _rows(self, api_id, body, field, cancelled=None):
        cont_yn, next_key, seen = 'N', '', set()
        while True:
            self._check_cancelled(cancelled)
            data, headers = self.query(api_id, '/api/dostk/stkinfo', body,
                                       cont_yn, next_key, True, cancelled)
            rows = data.get(field)
            if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
                raise RuntimeError(f'{api_id} 종목 목록 응답 형식 오류')
            yield from rows
            if headers.get('cont-yn', 'N') != 'Y':
                break
            next_key = headers.get('next-key', '')
            if not next_key or next_key in seen or not rows:
                raise RuntimeError(f'{api_id} 연속조회 중단: 전체 종목 조회 미완료')
            seen.add(next_key)
            cont_yn = 'Y'

    def _quote_rows(self, codes, cancelled, notify):
        """Retry smaller batches if a server rejects or truncates a large one.

        Never accept a partial universe. Two-code failures still stop the scan.
        Transport errors and broken pagination are not mistaken for size limits.
        """
        try:
            rows = list(self._rows('ka10095', {'stk_cd': '|'.join(codes)},
                                   'atn_stk_infr', cancelled))
        except KiwoomAPIError:
            if len(codes) <= 2:
                raise
            notify('묶음 요청 거부 · 작은 묶음으로 재조회 중')
            middle = len(codes) // 2
            return (self._quote_rows(codes[:middle], cancelled, notify) +
                    self._quote_rows(codes[middle:], cancelled, notify))
        received = {}
        for row in rows:
            code = str(row.get('stk_cd', '')).strip()
            if code not in codes:
                raise RuntimeError('요청과 다른 종목 시세 응답: 전체 조회 미완료')
            received[code] = row
        missing = [code for code in codes if code not in received]
        if missing:
            if len(codes) <= 2:
                raise RuntimeError(f'시세 {len(missing)}개 누락: 전체 조회 미완료. 추천을 생성하지 않습니다.')
            notify(f'묶음 응답 {len(missing)}개 누락 · 누락 종목 재조회 중')
            if len(missing) == len(codes):
                middle = len(codes) // 2
                return (self._quote_rows(codes[:middle], cancelled, notify) +
                        self._quote_rows(codes[middle:], cancelled, notify))
            rows = list(received.values()) + self._quote_rows(missing, cancelled, notify)
        return rows

    def scan(self, market='전체', progress=None, cancelled=None, include_etf=True):
        """Enumerate the selected markets before screening every listed symbol.

        Official specs: ka10099 (0=KOSPI, 10=KOSDAQ), ka10095
        (pipe-separated KRX codes). Live read-only probes confirmed 100 codes
        per request; smaller batches are retried if the server disagrees.
        """
        if market not in ('전체', 'KOSPI', 'KOSDAQ'):
            raise ValueError('시장은 전체, KOSPI, KOSDAQ 중 선택하세요.')
        notify = progress or (lambda text: None)
        markets = ['KOSPI', 'KOSDAQ'] if market == '전체' else [market]
        universe = {}
        for mrkt in markets:
            notify(f'{mrkt} 전체 종목 목록 수집 중')
            market_count = 0
            for row in self._rows('ka10099', {'mrkt_tp': {'KOSPI':'0', 'KOSDAQ':'10'}[mrkt]}, 'list', cancelled):
                code = str(row.get('code', '')).strip()
                if len(code) != 6 or not code.isascii() or not code.isalnum():
                    raise RuntimeError(f'{mrkt} 종목코드 형식 오류: 전체 조회 미완료')
                if code in universe and universe[code][0] != mrkt:
                    raise RuntimeError('시장별 종목 목록이 서로 일치하지 않습니다.')
                universe[code] = (mrkt, row.get('name') or code)
                market_count += 1
            if not market_count:
                raise RuntimeError(f'{mrkt} 전체 종목 목록이 비어 있습니다.')
        notify('ETF 분류 목록 수집 중')
        etfs = set()
        for row in self._rows('ka10099', {'mrkt_tp': '8'}, 'list', cancelled):
            code = str(row.get('code', '')).strip()
            if len(code) != 6 or not code.isascii() or not code.isalnum():
                raise RuntimeError('ETF 종목코드 형식 오류: 전체 조회 미완료')
            etfs.add(code)
        if not etfs:
            raise RuntimeError('ETF 분류 목록이 비어 있습니다. 포함 여부를 확인할 수 없습니다.')
        if not include_etf:
            universe = {code: meta for code, meta in universe.items() if code not in etfs}
        codes = list(universe)
        found = {}
        notify(f'ETF {"포함" if include_etf else "제외"} · 전체 대상 {len(codes):,}개 · 시세 조회 0개')
        for start in range(0, len(codes), self.QUOTE_BATCH_SIZE):
            self._check_cancelled(cancelled)
            batch = codes[start:start+self.QUOTE_BATCH_SIZE]
            received = {}
            for row in self._quote_rows(batch, cancelled, notify):
                code = str(row.get('stk_cd', '')).strip()
                if code not in batch:
                    raise RuntimeError('요청과 다른 종목 시세 응답: 전체 조회 미완료')
                amount = numeric(row.get('trde_prica'))  # million KRW -> eok
                price = numeric(row.get('cur_prc'), None, True)
                change = numeric(row.get('flu_rt'))
                volume = numeric(row.get('trde_qty'))
                if (price is None or change is None or volume is None or
                        amount is None or price < 0 or volume < 0 or amount < 0):
                    raise RuntimeError('종목 시세 필수값 누락: 전체 조회 미완료')
                mrkt, name = universe[code]
                # Keep receipt time separate from the exchange's quote time.
                quote_stamp = ''
                try:
                    quote_stamp = datetime.strptime(
                        str(row.get('dt', '')) + str(row.get('cntr_tm', '')),
                        '%Y%m%d%H%M%S').isoformat(timespec='seconds')
                except ValueError:
                    pass
                received[code] = Stock(
                    code, name, mrkt, price, change, amount / 100,
                    numeric(row.get('pred_trde_qty_pre')), numeric(row.get('mac')),
                    int(volume), 'KIWOOM', datetime.now().isoformat(timespec='seconds'), quote_stamp,
                    code in etfs)
            if set(received) != set(batch):
                missing = len(set(batch) - set(received))
                raise RuntimeError(f'시세 {missing}개 누락: 전체 조회 미완료. 추천을 생성하지 않습니다.')
            found.update(received)
            notify(f'전체 대상 {len(codes):,}개 · 시세 조회 {len(found):,}개 · 누락 0개')
        self._check_cancelled(cancelled)
        return sorted(found.values(), key=lambda x: x.trade_value_eok, reverse=True)


class Telegram:
    def __init__(self, bot_token, chat_id):
        self.bot_token = bot_token.strip()
        self.chat_id = str(chat_id).strip()
        if not self.bot_token or not self.chat_id:
            raise ValueError('텔레그램 Bot Token과 Chat ID가 필요합니다.')

    def send(self, text):
        resp = requests.post(f'https://api.telegram.org/bot{self.bot_token}/sendMessage',
                             json={'chat_id': self.chat_id, 'text': text[:3900]}, timeout=12)
        resp.raise_for_status()
        response = resp.json()
        if not response.get('ok'):
            raise RuntimeError('텔레그램 메시지 발송 실패')
        return response


def recommendation_text(rows, demo=False):
    lines = ['📊 KOSPI AI Trading · 종목 스크리닝',
             '⚠️ 가상 데이터 / 데모 테스트' if demo else '키움 시세 조회 · 투자권유 아님',
             '알림 생성: ' + datetime.now().strftime('%Y-%m-%d %H:%M'),
             '조회 당시 제공 시세 기준 · 장마감 후에도 조회·알림 가능', '']
    if not rows:
        lines.append('현재 설정 조건을 통과한 종목이 없습니다.')
    for i, row in enumerate(rows[:15], 1):
        kind = ' · ETF' if getattr(row, 'is_etf', False) else ''
        lines.append(f'{i}. {row.name} ({row.code}) · {row.market}{kind}\n'
                     f'  {row.price:,.0f}원 / {row.change_pct:+.1f}% / 점수 {getattr(row, "score", 0):.0f}\n'
                     f'  거래대금 {row.trade_value_eok:,.0f}억 · 시총 {row.cap_eok:,.0f}억' if row.cap_eok is not None
                     else f'{i}. {row.name} ({row.code}) · 시총 미수집')
        if not demo:
            lines.append('  시세 기준: ' + (getattr(row, 'quote_stamp', '') or 'API 제공 시각 확인 불가'))
    lines += ['', '이 버전은 알림 및 GUI 가상매매 전용입니다. 텔레그램 자동주문 미지원.']
    return '\n'.join(lines)
