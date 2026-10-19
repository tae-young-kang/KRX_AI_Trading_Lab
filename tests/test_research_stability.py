"""Offline TEST-half diagnostics: no selection based on holdout results."""
from copy import deepcopy
from datetime import date, timedelta

import pytest

from app.backtest import Bar
from app.research import evaluate_research
from app.research_stability import split_test_stability


def rows(*pairs):
    return [{'day': day, 'excess3': value} for day, value in pairs]


def test_first_and_second_halves_use_market_sessions_not_observed_pick_count():
    # With 8 TEST sessions, the midpoint is 10/04 -> 10/05,
    # regardless of how many pick days a strategy has in each half.
    sessions=[f'2026-10-{day:02d}' for day in range(1,9)]
    items=rows(('2026-10-01',2),('2026-10-04',4),
               ('2026-10-05',-1),('2026-10-08',-3))
    result=split_test_stability(sessions,items,min_days=2)
    assert result['test_early_days']==result['test_late_days']==2
    assert result['test_early_excess3']==3
    assert result['test_late_excess3']==-2
    assert result['test_late_gap3']==-5
    assert result['test_stability_note']=='후반 초과수익 비양수 전환'


def test_second_half_not_cherry_picked_based_on_available_returns():
    sessions=[f'2026-10-{day:02d}' for day in range(1,7)]
    # Only one observation in the early half, and three in late half.
    result=split_test_stability(
        sessions,
        rows(('2026-10-01',10),('2026-10-04',-2),
             ('2026-10-05',-2),('2026-10-06',-2)),min_days=2)
    assert result['test_early_sessions']==3
    assert result['test_late_sessions']==3
    assert result['test_early_days']==1
    assert result['test_late_days']==3
    assert result['test_stability_note']=='구간별 표본 부족'


def test_missing_returns_not_imputed_as_zero_and_no_data_is_unverified():
    sessions=['2026-10-01','2026-10-02','2026-10-03','2026-10-04']
    r=split_test_stability(sessions,rows(('2026-10-01',None),
                                         ('2026-10-04',float('nan'))))
    assert r['test_early_days']==0 and r['test_late_days']==0
    assert r['test_early_excess3'] is None and r['test_late_gap3'] is None
    assert r['test_stability_note']=='구간별 관측 부족'


def test_matured_values_are_one_observation_per_day_not_cumulative_trade_return():
    sessions=['2026-10-01','2026-10-02','2026-10-03','2026-10-04']
    r=split_test_stability(sessions,rows(('2026-10-01',1),
                                         ('2026-10-02',3),
                                         ('2026-10-03',5),
                                         ('2026-10-04',7)),min_days=2)
    assert r['test_early_excess3']==2
    assert r['test_late_excess3']==6
    assert r['test_late_gap3']==4
    assert r['test_stability_note']=='후반 초과수익 증가'


def test_duplicate_and_out_of_test_period_rejected():
    sessions=['2026-10-01','2026-10-02']
    with pytest.raises(ValueError):
        split_test_stability(sessions,rows(('2026-10-01',1),('2026-10-01',2)))
    with pytest.raises(ValueError):
        split_test_stability(sessions,rows(('2026-09-30',1)))
    with pytest.raises(ValueError):
        split_test_stability(['2026-10-01','2026-10-01'],[])
    with pytest.raises(ValueError):
        split_test_stability(sessions,[],min_days=0)


def test_overflowing_excess_gap_is_not_presented_as_finite_stability():
    days=['2026-10-01','2026-10-02']
    r=split_test_stability(days,rows((days[0],-1e308),(days[1],1e308)),min_days=1)
    assert r['test_late_gap3'] is None
    assert r['test_stability_note']=='구간별 관측 부족'


def synthetic_history(n=75):
    dates=[]
    current=date(2025,1,2)
    while len(dates)<n:
        if current.weekday()<5:
            dates.append(current.isoformat())
        current+=timedelta(days=1)
    output=[]
    for k in range(16):
        price=12000.0+1000*k
        for i,d in enumerate(dates):
            o=price*(1+0.001*(i%3))
            c=o*(1+(0.025 if i%6==k%6 else -0.005))
            volume=90000*(2.8 if i%8==k%8 else 1)
            output.append(Bar(d,f'{k:06d}',f'SYNTH{k}','KOSPI',
                              o,max(o,c)*1.005,min(o,c)*0.995,c,volume,
                              max(55,volume*c/1e8),5000+k*400))
            price=c
    return output


def test_research_variants_preserve_train_only_ranking_with_test_half_diagnostics():
    source=synthetic_history()
    result=evaluate_research(source)
    before_ids=[v['id'] for v in result['variants']]
    original=deepcopy(result)
    assert len(before_ids)==48
    assert len(set(before_ids))==48
    assert original['chosen_on_train_only']==result['chosen_on_train_only']
    for v in result['variants']:
        assert 'test_early_excess3' in v and 'test_late_excess3' in v
        assert 'test_early_days' in v and 'test_late_days' in v
        assert v['test_early_days']+v['test_late_days']<=v['test_days']
        assert v['test_early_sessions']+v['test_late_sessions'] > 0
    chosen=next((v['id'] for v in result['variants'] if v['train_days']>=12),None)
    assert result['chosen_on_train_only']==chosen
