"""Offline regression: a missing daily bar cannot be bridged in factor lookback."""
from datetime import date, timedelta

from app.backtest import Bar
from app.research import build_features


def market_days(count=60):
    result = []
    current = date(2025, 1, 2)
    while len(result) < count:
        if current.weekday() < 5:
            result.append(current.isoformat())
        current += timedelta(days=1)
    return result


def make_bar(day, code, *, volume=1000, market='KOSPI'):
    # Entirely synthetic, no broker/network data.
    return Bar(day, code, 'synthetic', market, 10000, 10100, 9900, 10000,
               volume, 100, 5000)


def test_missing_session_blocks_stale_volume_ratio_until_twenty_new_sessions():
    days = market_days()
    records = []
    for i, day in enumerate(days):
        records.append(make_bar(day, '900001'))  # Keeps full market calendar.
        if i != 30:
            records.append(make_bar(day, '900002', volume=100000 if i == 31 else 1000))
    by_day, quality = build_features(records, window=20)

    def codes(index):
        return {row['code'] for row in by_day.get(('KOSPI', days[index]), ())}

    assert '900002' in codes(29)
    assert '900001' in codes(31)
    # The first daily bar after the gap must not use an old 20-day window.
    assert '900002' not in codes(31)
    assert '900002' not in codes(50)
    # Twenty consecutive PRIOR market sessions restore factor eligibility.
    assert '900002' in codes(51)
    assert quality['invalid_history'] == 20


def test_other_markets_keep_independent_session_calendars():
    days = market_days(45)
    rows = [make_bar(day, '900001') for day in days]
    rows.extend(make_bar(day, '900003', market='KOSDAQ') for day in days[::2])
    data, quality = build_features(rows, window=20)
    assert '900001' in {r['code'] for r in data[('KOSPI',days[-1])]}
    # The other market contains only every second KOSPI date; those are
    # consecutive within its own supplied market calendar.
    assert '900003' in {r['code'] for r in data[('KOSDAQ',days[42])]}
    assert quality['invalid_history'] == 0
