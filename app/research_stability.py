"""Chronological TEST-period stability diagnostics for screening research.

Descriptions only: no strategy re-ranking, live-data access or trade execution.
The input excess returns are already computed per evaluated recommendation day
against the same-market benchmark; never substitute net top-signal returns.
"""
from math import isfinite
from statistics import mean


def split_test_stability(test_market_days, evaluated_days, *, min_days=5):
    """Split TEST market sessions once, independent of observed strategy picks.

    TEST dates are divided by calendar-session order, not by each variant's
    observed days. This prevents cherry-picking equally sized groups of good
    observed outcomes. Pending/unmatured dates are NOT counted as 0% returns.
    """
    if type(min_days) is not int or min_days < 1:
        raise ValueError('최소 평가일 수는 양의 정수여야 합니다.')
    sessions = sorted(set(test_market_days))
    if len(sessions) != len(test_market_days):
        raise ValueError('TEST 거래일 입력은 중복될 수 없습니다.')
    cut = (len(sessions) + 1) // 2
    first = set(sessions[:cut])
    second = set(sessions[cut:])
    out = [[], []]
    seen = set()
    for item in evaluated_days:
        day = item['day']
        excess = item.get('excess3')
        if day in seen:
            raise ValueError('같은 전략의 동일 날짜 초과수익은 중복될 수 없습니다.')
        seen.add(day)
        if day not in first and day not in second:
            raise ValueError('TEST 밖의 평가일을 혼합할 수 없습니다.')
        if type(excess) not in (int, float) or not isfinite(excess):
            continue
        out[0 if day in first else 1].append(float(excess))
    early, late = out
    early_avg = round(mean(early), 4) if early else None
    late_avg = round(mean(late), 4) if late else None
    delta = late_avg-early_avg if early_avg is not None and late_avg is not None else None
    gap = round(delta, 4) if delta is not None and isfinite(delta) else None
    if early_avg is None or late_avg is None or gap is None:
        label = '구간별 관측 부족'
    elif len(early) < min_days or len(late) < min_days:
        label = '구간별 표본 부족'
    elif early_avg > 0 and late_avg <= 0:
        label = '후반 초과수익 비양수 전환'
    elif gap < 0:
        label = '후반 초과수익 감소'
    elif gap > 0:
        label = '후반 초과수익 증가'
    else:
        label = '후반 초과수익 변화 없음'
    return {
        'test_early_excess3': early_avg,
        'test_late_excess3': late_avg,
        'test_early_days': len(early),
        'test_late_days': len(late),
        'test_early_sessions': len(first),
        'test_late_sessions': len(second),
        'test_late_gap3': gap,
        'test_stability_note': label,
    }
