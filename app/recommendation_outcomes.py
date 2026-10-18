"""Forward price observations, not fills, net returns or strategy forecasts."""
from dataclasses import dataclass
from datetime import date, datetime
from math import isfinite
from app.trading_clock import KST


def session_day(value):
    """One canonical representation across SQL, cache keys and evidence hashes."""
    try:
        parsed=date.fromisoformat(value)
        if type(value) is not str or value!=parsed.isoformat():
            raise ValueError()
        return parsed
    except (TypeError,ValueError):
        raise ValueError('날짜는 YYYY-MM-DD 형식으로 확인해 주세요.') from None


@dataclass(frozen=True)
class SessionEvidence:
    days: tuple[str, ...]
    begin: str
    end: str
    source: str  # DEMO, USER_CONFIRMED or OFFICIAL; never inferred from weekdays.

    def validate(self, mode):
        if mode not in ('DEMO','MOCK','REAL_QUOTES') or not isinstance(self.days,(tuple,list)):
            raise ValueError('자료 모드와 거래일 목록을 확인해 주세요.')
        if self.source not in ('DEMO','USER_CONFIRMED','OFFICIAL'):
            raise ValueError('거래일 자료의 출처를 확인해 주세요.')
        if (mode == 'DEMO') != (self.source == 'DEMO'):
            raise ValueError('가상 연습과 실제 거래일 자료를 섞을 수 없어요.')
        begin, end = session_day(self.begin), session_day(self.end)
        parsed = tuple(session_day(day) for day in self.days)
        if begin > end or parsed != tuple(sorted(set(parsed))) or any(not begin <= day <= end for day in parsed):
            raise ValueError('확인한 거래일 범위와 목록이 맞지 않아요.')
        return begin,end,parsed


@dataclass(frozen=True)
class ForwardObservation:
    horizon: int
    target_day: str | None
    state: str
    close: float | None = None
    change_pct: float | None = None


def evaluate(*, mode, code, market, entry_price, recommended_at, bars,
             sessions, available_through, price_source):
    if mode not in ('DEMO','MOCK','REAL_QUOTES'):
        raise ValueError('추천 모드를 확인해 주세요.')
    if price_source not in ('DEMO','KRX','CSV_USER') or (mode == 'DEMO') != (price_source == 'DEMO'):
        raise ValueError('추천 모드와 가격 자료 출처가 맞지 않아요.')
    if (type(entry_price) not in (int,float) or not isfinite(entry_price) or entry_price <= 0
            or not isinstance(recommended_at,datetime) or recommended_at.tzinfo is None):
        raise ValueError('추천 당시 가격과 한국 시각이 필요해요.')
    signal_day = recommended_at.astimezone(KST).date()
    begin,end,days = sessions.validate(mode)
    day_set = set(days)
    through = session_day(available_through)
    if not begin <= signal_day <= end or not begin <= through <= end or through < signal_day:
        raise ValueError('추천일과 자료 확인일을 포함하는 거래일 범위가 필요해요.')
    prices = {}
    for bar in bars:
        if bar.code != code or bar.market != market:
            continue
        day = session_day(bar.date)
        if day > through:  # Future data supplied by caller cannot leak into results.
            continue
        if (type(bar.close) not in (int,float) or not isfinite(bar.close) or bar.close <= 0
                or day in prices or day not in day_set):
            raise ValueError('중복·비정상 가격 또는 거래일과 다른 가격 자료가 있어요.')
        prices[day] = float(bar.close)
    future = [day for day in days if day > signal_day]
    out = []
    for horizon in (1,3,5):
        if len(future) < horizon:
            out.append(ForwardObservation(horizon,None,'CALENDAR_MISSING'))
            continue
        target = future[horizon-1]
        if target > through:
            out.append(ForwardObservation(horizon,target.isoformat(),'NOT_DUE'))
        elif target not in prices:
            out.append(ForwardObservation(horizon,target.isoformat(),'PRICE_MISSING'))
        else:
            close = prices[target]
            change = (close/entry_price-1)*100
            if not isfinite(change):
                raise ValueError('가격 변화 계산 범위를 벗어났어요. 입력 가격을 확인해 주세요.')
            out.append(ForwardObservation(horizon,target.isoformat(),'OBSERVED',close,change))
    return tuple(out)
