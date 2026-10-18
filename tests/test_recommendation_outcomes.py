"""Explicit synthetic sessions and prices, no trading calendar/network guesses."""
from datetime import datetime
from dataclasses import replace
import pytest
from app.backtest import Bar
from app.trading_clock import KST
from app.recommendation_outcomes import SessionEvidence, evaluate


DAYS = ('2026-10-19','2026-10-20','2026-10-22','2026-10-23','2026-10-26','2026-10-27')
CALENDAR = SessionEvidence(DAYS,DAYS[0],DAYS[-1],'DEMO')


def prices():
    return [Bar(day,'900001','시험','KOSPI',100,120,80,100+i*2,1000) for i,day in enumerate(DAYS)]


def run(**changes):
    args = dict(mode='DEMO',code='900001',market='KOSPI',entry_price=100,
        recommended_at=datetime(2026,10,19,10,tzinfo=KST),bars=prices(),sessions=CALENDAR,
        available_through=DAYS[-1],price_source='DEMO')
    args.update(changes)
    return evaluate(**args)


def test_explicit_sessions_count_without_weekday_inference():
    results = run()
    assert [r.target_day for r in results] == [DAYS[1],DAYS[3],DAYS[5]]
    assert [r.close for r in results] == [102,106,110]
    assert results[0].change_pct == pytest.approx(2)
    assert all(r.state=='OBSERVED' for r in results)


def test_missing_symbol_day_is_never_replaced_with_next_available_bar():
    rows = [bar for bar in prices() if bar.date != DAYS[1]]
    results = run(bars=rows)
    assert results[0].state == 'PRICE_MISSING' and results[0].change_pct is None
    assert results[0].target_day == DAYS[1] and results[1].close == 106


def test_future_prices_never_leak_into_pending_outcomes():
    results = run(available_through=DAYS[1])
    assert results[0].state == 'OBSERVED'
    assert all(r.state=='NOT_DUE' and r.change_pct is None for r in results[1:])


def test_short_calendar_cannot_be_confused_with_price_pending():
    calendar = SessionEvidence(DAYS[:2],DAYS[0],DAYS[1],'DEMO')
    results = run(sessions=calendar,available_through=DAYS[1])
    assert results[1].state == 'CALENDAR_MISSING' and results[1].target_day is None


@pytest.mark.parametrize('change', [dict(price_source='KRX'),dict(mode='MOCK'),
    dict(sessions=replace(CALENDAR,source='OFFICIAL')),dict(entry_price=float('nan')),
    dict(recommended_at=datetime(2026,10,19)),dict(available_through='2026-10-18'),
    dict(sessions=replace(CALENDAR,days=tuple(reversed(DAYS))))])
def test_mixed_modes_and_unverified_time_ranges_rejected(change):
    with pytest.raises(ValueError): run(**change)


@pytest.mark.parametrize('change',['duplicate','zero','non_session'])
def test_conflicting_price_data_does_not_produce_observations(change):
    bars = prices()
    if change=='duplicate': bars.append(bars[1])
    elif change=='zero': bars[1]=replace(bars[1],close=0)
    else: bars[1]=replace(bars[1],date='2026-10-21')
    with pytest.raises(ValueError): run(bars=bars)


def test_other_symbol_or_market_never_fills_a_missing_price():
    bars = [replace(bar,market='KOSDAQ') for bar in prices()]
    assert all(r.state=='PRICE_MISSING' for r in run(bars=bars))


def test_finite_input_prices_cannot_store_infinite_percentage():
    bars=[replace(bar,close=1e308) for bar in prices()]
    with pytest.raises(ValueError,match='계산 범위'):
        run(entry_price=1e-308,bars=bars)
