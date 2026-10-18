"""Exact fill-based exit decisions only; no client, order or execution permission."""
from dataclasses import dataclass
from datetime import datetime
import re


def validate_policy(take_bps, stop_bps):
    if any(type(value) is not int or not 1<=value<10000 for value in (take_bps,stop_bps)):
        raise ValueError('익절·손절은 0.01% 이상 100% 미만으로 입력해 주세요.')


@dataclass(frozen=True)
class FilledPosition:
    mode: str
    code: str
    venue: str
    filled_quantity: int
    filled_value_won: int
    sold_quantity: int = 0
    pending_sell_quantity: int = 0
    reconciled: bool = False


@dataclass(frozen=True)
class ObservedPrice:
    mode: str
    code: str
    venue: str
    price_won: int
    quote_at: datetime
    fetched_at: datetime


@dataclass(frozen=True)
class ProtectionDecision:
    state: str
    quantity: int
    reason: str
    executable: bool = False


def evaluate(position, quote, *, now, take_bps=200, stop_bps=300):
    """Trigger on gross average fill price; fees and actual exit fills are separate."""
    validate_policy(take_bps,stop_bps)
    def blocked(reason):
        return ProtectionDecision('BLOCKED',0,reason)
    if (not isinstance(position,FilledPosition) or position.mode not in ('DEMO','MOCK','REAL_QUOTES')
            or not isinstance(position.code,str) or not re.fullmatch(r'[A-Za-z0-9]{6}',position.code)
            or position.venue not in ('KRX','NXT')):
        return blocked('체결 기록의 모드·종목·시장을 확인해야 해요.')
    quantities=(position.filled_quantity,position.sold_quantity,position.pending_sell_quantity)
    if (any(type(value) is not int or not 0<=value<=10**12 for value in quantities)
            or type(position.filled_value_won) is not int or not 0<=position.filled_value_won<=10**18
            or (position.filled_quantity==0)!=(position.filled_value_won==0)
            or position.sold_quantity+position.pending_sell_quantity>position.filled_quantity):
        return blocked('체결 수량·금액·매도 잔량을 확인해야 해요.')
    if position.reconciled is not True:
        return blocked('체결과 보유 잔량을 먼저 대조해야 해요.')
    remaining=position.filled_quantity-position.sold_quantity
    if not remaining:
        return ProtectionDecision('NO_POSITION',0,'확인된 보유 수량이 없어요.')
    if position.pending_sell_quantity:
        return blocked('이미 매도 대기 수량이 있어 새 매도 판단을 보류해요.')
    if (not isinstance(quote,ObservedPrice) or (quote.mode,quote.code,quote.venue)!=
            (position.mode,position.code,position.venue) or type(quote.price_won) is not int
            or not 0<quote.price_won<=10**15):
        return blocked('같은 모드·종목·시장의 유효 시세가 필요해요.')
    stamps=(now,quote.quote_at,quote.fetched_at)
    if any(not isinstance(stamp,datetime) or stamp.tzinfo is None or stamp.utcoffset() is None for stamp in stamps):
        return blocked('시세 시각과 수신 시각에 시간대가 필요해요.')
    if (not 0<=(now-quote.quote_at).total_seconds()<=30
            or not 0<=(now-quote.fetched_at).total_seconds()<=30 or quote.quote_at>quote.fetched_at):
        return blocked('오래됐거나 순서가 맞지 않는 시세예요.')
    value=quote.price_won*position.filled_quantity*10000
    if value>=position.filled_value_won*(10000+take_bps):
        return ProtectionDecision('TAKE_PROFIT',remaining,'체결 평균가 기준 익절 조건에 도달했어요.')
    if value<=position.filled_value_won*(10000-stop_bps):
        return ProtectionDecision('STOP_LOSS',remaining,'체결 평균가 기준 손절 조건에 도달했어요.')
    return ProtectionDecision('HOLD',remaining,'아직 익절·손절 조건 전이에요.')
