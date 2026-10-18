from dataclasses import replace
from app.backtest import Bar
from app.recommendation_outcomes import SessionEvidence
from app.theme_one import detect


DAYS=('2026-10-08','2026-10-12','2026-10-13','2026-10-14','2026-10-15','2026-10-16','2026-10-19','2026-10-20')


def run(hits,through='2026-10-19',missing=None):
    bars=[Bar(day,'900001','연습','KOSPI',100,110 if day in hits else 101,99,100,1000,100,4000)
          for day in DAYS if day!=missing]
    return detect(code='900001',market='KOSPI',bars=bars,
                  sessions=SessionEvidence(DAYS,DAYS[0],DAYS[-1],'DEMO'),
                  through=through,mode='DEMO',price_source='DEMO')


def test_six_sessions_skip_weekend_and_roll_out_oldest():
    hits={'2026-10-12','2026-10-14','2026-10-19'}
    result=run(hits)
    assert result['matched'] and len(result['window'])==6
    assert [h['day'] for h in result['hits']]==sorted(hits)
    assert not run(hits,'2026-10-20')['matched']
    assert run(hits|{'2026-10-20'},'2026-10-20')['matched']


def test_all_matching_days_and_missing_evidence_are_preserved():
    result=run(set(DAYS))
    assert len(result['hits'])==6 and result['cap_eok']==4000
    assert run(set(DAYS),missing='2026-10-15')['status']=='PRICE_MISSING'
    assert run(set(DAYS),'2026-10-16')['status']=='CALENDAR_MISSING'
