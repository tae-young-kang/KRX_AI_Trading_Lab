"""Explicit KST session plans; no guessed holidays and no live execution gate."""
from dataclasses import dataclass, field
from datetime import datetime, date, time, timedelta, timezone

KST = timezone(timedelta(hours=9))
REGULAR_WINDOWS = {
    'KRX': ((time(9), time(15, 30)),),
    'NXT': ((time(8), time(8, 50)), (time(9, 0, 30), time(15, 20)),
            (time(15, 40), time(20))),
}


@dataclass
class TradingCalendar:
    # A weekday is NOT evidence that the exchange is open. Unknown dates close.
    open_dates: set[date] = field(default_factory=set)
    overrides: dict = field(default_factory=dict)

    def is_open(self, moment: datetime, venue: str):
        if moment.tzinfo is None:
            raise ValueError('시간대가 포함된 시각이 필요합니다.')
        if venue not in REGULAR_WINDOWS:
            raise ValueError('지원하지 않는 거래시장입니다.')
        local = moment.astimezone(KST)
        if local.date() not in self.open_dates:
            return False
        windows = self.overrides.get((local.date(), venue), REGULAR_WINDOWS[venue])
        return any(start <= local.time() < end for start, end in windows)


PRICE_BASIS_NOTICE = (
    '현재가는 API가 제공한 마지막 시세입니다. 정규장 종가로 확정한 가격이 아닙니다. '
    '시세 시각과 조회 시각은 다르며, 이 화면만으로 체결 거래소를 확정하지 않습니다.'
)
