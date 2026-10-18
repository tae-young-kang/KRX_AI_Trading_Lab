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


def test_exact_threshold_and_all_hit_sum_alert():
    from app.theme_one import alert_text
    bars=[Bar(day,'900001','연습','KOSPI',100,high,99,100,1000,100,4000)
          for day,high in zip(DAYS,[100,105,110,120,130,101,101,101])]
    result=detect(code='900001',market='KOSPI',bars=bars,
                  sessions=SessionEvidence(DAYS,DAYS[0],DAYS[-1],'DEMO'),
                  through='2026-10-19',mode='DEMO',price_source='DEMO')
    assert len(result['hits'])==4 and result['high_sum']==65 and result['sum_band']=='EQUAL'
    text=alert_text(result,nxt_eligible=True,nxt_asof='2026-10-19',nxt_source='연습 목록')
    assert '4.' in text and '65%와 같음' in text and 'NXT 거래 가능 ·' in text
    assert '2026-10-19(월)' in text and '수익률이 아닙니다' in text
    assert '미확인' in alert_text(result,nxt_eligible=False,nxt_asof='2026-10-16',nxt_source='old')
    assert 'KRX 전용' in alert_text(result,nxt_eligible=False,nxt_asof='2026-10-19',nxt_source='연습 목록')


def test_value_and_high_thresholds_must_hold_on_same_day():
    import pytest
    for high,value,expected in [(105,100,True),(104.999,100,False),(105,99.999,False)]:
        bars=[Bar(day,'900001','연습','KOSPI',100,high,99,100,1000,value,4000) for day in DAYS]
        result=detect(code='900001',market='KOSPI',bars=bars,sessions=SessionEvidence(DAYS,DAYS[0],DAYS[-1],'DEMO'),through=DAYS[-2],mode='DEMO',price_source='DEMO')
        assert result['matched']==expected


def test_nonfinite_or_inconsistent_data_rejected_and_future_ignored():
    import pytest
    bars=[Bar(day,'900001','연습','KOSPI',100,110,99,100,1000,100,4000) for day in DAYS]
    args=dict(code='900001',market='KOSPI',sessions=SessionEvidence(DAYS,DAYS[0],DAYS[-1],'DEMO'),through=DAYS[-2],mode='DEMO',price_source='DEMO')
    for patch in [dict(high=float('inf')),dict(close=0),dict(trade_value_eok=None),dict(low=101),dict(name='')]:
        broken=bars.copy();broken[2]=replace(broken[2],**patch)
        with pytest.raises(ValueError): detect(bars=broken,**args)
    bars[-1]=replace(bars[-1],high=float('nan'))
    assert detect(bars=bars,**args)['matched']
    with pytest.raises(ValueError,match='중복'): detect(bars=bars+[bars[2]],**args)


def test_overflowing_aggregate_is_not_persistable_result():
    import pytest
    bars=[Bar(day,'900001','연습','KOSPI',100,110,99,100,1000,1e308,4000) for day in DAYS]
    with pytest.raises(ValueError,match='합계'):
        detect(code='900001',market='KOSPI',bars=bars,sessions=SessionEvidence(DAYS,DAYS[0],DAYS[-1],'DEMO'),through=DAYS[-2],mode='DEMO',price_source='DEMO')
