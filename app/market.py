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

    def as_dict(self):
        return asdict(self)


def numeric(obj, default=None, abs_value=False):
    try:
        raw = str(obj).replace(',', '').strip()
        if not raw:
            return default
        value = float(raw)
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

    def scan(self, market='전체', candidate_limit=30):
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
        return result[:candidate_limit]


class KiwoomMarket:
    """Read-only REST screener. KRX quotes only for consistent early-stage data tests."""
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

    def query(self, api_id, path, body):
        if not self.token or time.time() - self.token_ts > 20 * 3600:
            self.login()
        delay = 1.1 if 'mockapi' in self.url else 0.26
        interval = time.monotonic() - self.last_api_call
        if interval < delay:
            time.sleep(delay - interval)
        self.last_api_call = time.monotonic()
        resp = self.session.post(self.url + path, headers={
            'authorization': f'Bearer {self.token}', 'api-id': api_id,
            'Content-Type': 'application/json;charset=UTF-8', 'cont-yn': 'N', 'next-key': ''
        }, json=body, timeout=12)
        resp.raise_for_status()
        data = resp.json()
        rc = data.get('return_code', data.get('returnCode', 0))
        if str(rc) not in ('0', '20'):
            raise RuntimeError(f'{api_id} 오류 ({rc}): {data.get("return_msg", data.get("returnMsg", ""))}')
        return data

    def scan(self, market='전체', candidate_limit=30):
        markets = ['KOSPI', 'KOSDAQ'] if market == '전체' else [market]
        found = {}
        for mrkt in markets:
            market_id = {'KOSPI': '001', 'KOSDAQ': '101'}[mrkt]
            # Official ka10030 example: trde_amt is generally in million KRW.
            raw = self.query('ka10030', '/api/dostk/rkinfo', {
                'mrkt_tp': market_id, 'sort_tp': '3', 'mang_stk_incls': '4',
                'crd_tp': '0', 'trde_qty_tp': '0', 'pric_tp': '0',
                'trde_prica_tp': '0', 'mrkt_open_tp': '0', 'stex_tp': '1'
            })
            for row in raw.get('tdy_trde_qty_upper', []):
                code = row.get('stk_cd', '').split('_')[0]
                if not code or code in found:
                    continue
                amount_million = numeric(row.get('trde_amt'))
                found[code] = Stock(
                    code, row.get('stk_nm', code), mrkt,
                    numeric(row.get('cur_prc'), 0, True), numeric(row.get('flu_rt'), 0),
                    amount_million / 100 if amount_million is not None else 0,
                    numeric(row.get('pred_rt')), None,
                    int(numeric(row.get('trde_qty'), 0) or 0), 'KIWOOM',
                    datetime.now().isoformat(timespec='seconds')
                )
        ordered = sorted(found.values(), key=lambda x: x.trade_value_eok, reverse=True)[:candidate_limit]
        # cap field's official documentation lists market cap as 100M KRW (eok) unit.
        for stock in ordered:
            info = self.query('ka10001', '/api/dostk/stkinfo', {'stk_cd': stock.code})
            stock.cap_eok = numeric(info.get('mac'))
            if stock.cap_eok is None:
                # Important: unknown cap is NEVER treated as zero or a pass.
                pass
        return ordered


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
             datetime.now().strftime('%Y-%m-%d %H:%M'), '']
    if not rows:
        lines.append('현재 설정 조건을 통과한 종목이 없습니다.')
    for i, row in enumerate(rows[:15], 1):
        lines.append(f'{i}. {row.name} ({row.code}) · {row.market}\n'
                     f'  {row.price:,.0f}원 / {row.change_pct:+.1f}% / 점수 {getattr(row, "score", 0):.0f}\n'
                     f'  거래대금 {row.trade_value_eok:,.0f}억 · 시총 {row.cap_eok:,.0f}억' if row.cap_eok is not None
                     else f'{i}. {row.name} ({row.code}) · 시총 미수집')
    lines += ['', '이 버전은 알림 및 GUI 가상매매 전용입니다. 텔레그램 자동주문 미지원.']
    return '\n'.join(lines)
