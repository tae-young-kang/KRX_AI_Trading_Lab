"""Descriptive summaries cannot inflate repeated scans into independent samples."""
from copy import deepcopy
from app.config import DEFAULTS
from app.recommendation_overview import groups
from test_recommendation_reports import store
from test_recommendations import candidates,save
from test_recommendation_outcomes import DAYS,prices
from test_telegram_orders import sandbox,update


def test_same_day_same_condition_and_symbol_uses_first_recommendation_not_best_result(tmp_path):
    db,evidence=store(tmp_path)
    db.review_recommendation(**evidence)
    rows=candidates()
    rows[0].stock.price=2000
    sid=save(db,rows,deepcopy(DEFAULTS))
    with db.connect() as record:
        record.execute('UPDATE recommendation_batches SET created_at=? WHERE scan_id=?',
                       ('2026-10-19T11:00:00+09:00',sid))
    db.review_recommendation(**{**evidence,'scan_id':sid})
    with db.connect() as record:
        data,_=groups(record,'DEMO')
    observed=[g for g in data if g['source']=='DEMO']
    assert len(observed)==1 and observed[0]['count']==1
    first=observed[0]['horizons'][1]
    assert first['observed']==1 and first['down']==1 and first['up']==0
    assert first['mean']==(102/1000-1)*100  # First entry, never the later entry.
    assert sum(g['count'] for g in data)==3
    text=db.recommendation_overview('DEMO')
    assert '관측 20개 미만' in text and '미평가' in text and len(text)<4096


def test_missing_pending_and_unreviewed_are_not_losses_or_zero_returns(tmp_path):
    db,evidence=store(tmp_path)
    db.review_recommendation(**{**evidence,'bars':[],'available_through':DAYS[1]})
    with db.connect() as record: data,_=groups(record,'DEMO')
    observed=next(g for g in data if g['source']=='DEMO')
    assert observed['horizons'][1]['missing']==1
    assert observed['horizons'][3]['waiting']==1
    assert all(v['mean'] is None and v['down']==0 for v in observed['horizons'].values())
    assert '평균 미확인' in db.recommendation_overview('DEMO')
    assert '집계할 추천 기록이 없어요' in db.recommendation_overview('MOCK')


def test_changed_filter_remains_separate_and_unknown_times_excluded(tmp_path):
    db,_=store(tmp_path)
    cfg={**DEFAULTS,'include_etf':False}
    sid=save(db,candidates(),cfg)
    with db.connect() as record:
        record.execute('UPDATE recommendation_batches SET created_at=? WHERE scan_id=?',('2026-10-19T11:00:00+09:00',sid))
        data,_=groups(record,'DEMO')
        assert len(data)==2
        record.execute('UPDATE recommendation_batches SET created_at=? WHERE scan_id=?',('2026-10-19T11:00:00',sid))
        data,skipped=groups(record,'DEMO')
        assert len(data)==1 and skipped==3


def test_telegram_summary_is_authenticated_and_readonly_after_stop(tmp_path,sandbox):
    db,_=store(tmp_path)
    service,broker,_,_=sandbox
    service.recommendation_overview=lambda:db.recommendation_overview('DEMO')
    bad=update(sandbox,1,'/추천요약');bad['message']['chat']['type']='group'
    assert service.handle(bad) is None
    service.handle(update(sandbox,2,'/중지'))
    event=update(sandbox,3,'/추천요약')
    reply=service.handle(event)
    assert '가격 변화는 비용' in reply and service.handle(event)==reply and broker.calls==[]
