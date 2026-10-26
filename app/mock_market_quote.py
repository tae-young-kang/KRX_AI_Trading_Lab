"""Parse one ka10095 MOCK KRX response; no client or order execution."""
from datetime import datetime
import re

from app.kiwoom_mock_contract import ContractError, _success
from app.mock_market_budget import UpperPrice
from app.protection_plan import ObservedPrice
from app.trading_clock import KST


def parse_quote(payload, headers, *, code, started_at, fetched_at):
    """Caller must supply a response from its authenticated MOCK KRX owner.

    Receipt time never substitutes for the trade date/time. A fresh bid time
    cannot make a stale last trade fresh. This parser does not prove provenance.
    """
    error = '종목·날짜·체결 시각이 확인된 최신 모의 시세가 필요해요.'
    if not isinstance(code, str) or not re.fullmatch(r'[A-Za-z0-9]{6}', code):
        raise ContractError(error)
    stamps = (started_at, fetched_at)
    if any(not isinstance(t, datetime) or t.tzinfo is None or t.utcoffset() is None for t in stamps):
        raise ContractError(error)
    if not 0 <= (fetched_at-started_at).total_seconds() <= 5:
        raise ContractError(error)
    _success(payload)
    if not isinstance(headers, dict):
        raise ContractError(error)
    metadata = {str(k).lower(): v for k, v in headers.items()}
    if (metadata.get('api-id', 'ka10095') != 'ka10095'
            or metadata.get('cont-yn') != 'N' or metadata.get('next-key', '') != ''):
        raise ContractError(error)
    rows = payload.get('atn_stk_infr')
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise ContractError(error)
    row = rows[0]
    if row.get('stk_cd') != code:
        raise ContractError(error)
    day, clock = row.get('dt'), row.get('cntr_tm')
    if (not isinstance(day, str) or not re.fullmatch(r'[0-9]{8}', day)
            or not isinstance(clock, str) or not re.fullmatch(r'[0-9]{6}', clock)):
        raise ContractError(error)
    try:
        event = datetime.strptime(day+clock, '%Y%m%d%H%M%S').replace(tzinfo=KST)
    except ValueError:
        raise ContractError(error) from None
    if (event.date() != fetched_at.astimezone(KST).date()
            or started_at.astimezone(KST).date() != event.date()
            or not 0 <= (fetched_at-event).total_seconds() <= 5):
        raise ContractError(error)

    def price(field):
        value = row.get(field)
        # Kiwoom price signs describe movement, not negative KRW prices.
        if not isinstance(value, str) or not re.fullmatch(r'[+-]?[0-9]{1,12}', value):
            raise ContractError(error)
        result = abs(int(value))
        if not 0 < result < 10**12:
            raise ContractError(error)
        return result

    current, upper = price('cur_prc'), price('upl_pric')
    if current > upper:
        raise ContractError(error)
    return (ObservedPrice('MOCK', code, 'KRX', current, event, fetched_at),
            UpperPrice('MOCK', code, 'KRX', event.date(), upper, fetched_at))
