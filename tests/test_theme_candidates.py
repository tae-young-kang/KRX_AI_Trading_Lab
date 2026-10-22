"""Query-plan equivalence against the real detector; synthetic data only."""
from dataclasses import replace
from app.backtest import Bar
from app.recommendation_outcomes import SessionEvidence
from app.theme_one import detect
from app.theme_candidates import plan_candidates
from test_kiwoom_daily import DAYS


def test_plan_never_excludes_a_stock_that_can_match_today():
    universe=[('KOSPI','900001','가상')]
    sessions=SessionEvidence(tuple(DAYS),DAYS[0],DAYS[-1],'DEMO')
    for mask in range(32):
        past=[Bar(DAYS[0],'900001','가상','KOSPI',100,100,100,100,1,100,4000)]
        for i,day in enumerate(DAYS[1:-1]):
            past.append(Bar(day,'900001','가상','KOSPI',100,105 if mask&(1<<i) else 104,99,100,1,100,4000))
        plan=plan_candidates(universe,past,DAYS[:-1])
        for today_high,today_value in ((104,100),(105,100),(120,99)):
            today=Bar(DAYS[-1],'900001','가상','KOSPI',100,today_high,99,100,1,today_value,4000)
            result=detect(code='900001',market='KOSPI',bars=past+[today],sessions=sessions,through=DAYS[-1],mode='DEMO',price_source='DEMO')
            if result['matched']:assert plan['candidates']==universe
        assert bool(plan['candidates'])==(mask.bit_count()>=2)


def test_missing_history_or_turnover_always_stays_in_plan():
    universe=[('KOSPI','900001','가상'),('KOSDAQ','900002','신규')]
    bars=[Bar(d,'900001','가상','KOSPI',100,100,99,100,1,99,4000) for d in DAYS[:-1]]
    bars[-1]=replace(bars[-1],trade_value_eok=None)
    plan=plan_candidates(universe,bars,DAYS[:-1])
    assert plan['candidates']==universe and len(plan['unknown_history'])==2
    assert plan['excluded']==[]
