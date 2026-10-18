import json
from datetime import date
import pytest
from app.backtest import Bar
from app.krx_history import HistoryStore
from app.recommendation_cache import evaluate_selection,evaluate_saved
from app.storage import Database
from test_recommendation_import import package, TODAY
from test_recommendation_reports import store


def test_cached_selection_evaluates_without_network_and_is_idempotent(tmp_path):
    db,_ = store(tmp_path)
    history = HistoryStore(tmp_path/'history.db')
    data = package()
    history.insert_bars([Bar(row['day'],row['code'],'시험',row['market'],row['close'],
        row['close'],row['close'],row['close'],1000,100,4000) for row in data['prices']], 'DEMO')
    selection = dict(version=1,mode='DEMO',source='DEMO',through=data['available_through'],calendar=data['calendar'])
    path = tmp_path/'selection.json'
    path.write_text(json.dumps(selection),encoding='utf8')
    result = evaluate_selection(db,history,path,'DEMO',today=TODAY)
    assert result['recommendations'] == 3 and result['added'] == 3
    assert evaluate_selection(db,history,path,'DEMO',today=TODAY)['added'] == 0
    path.unlink()
    reopened=Database(db.path)
    assert evaluate_saved(reopened,history,'DEMO',today=TODAY)['added']==0
    with pytest.raises(ValueError,match='먼저'):
        evaluate_saved(reopened,history,'MOCK',today=TODAY)
    path.write_text(json.dumps(selection),encoding='utf8')
    assert '가격 자료 없음' in db.recommendation_summary('DEMO',performance=True)
    with pytest.raises(ValueError,match='오늘 이전'):
        evaluate_selection(db,history,path,'DEMO',today=date.fromisoformat(selection['through']))


def test_saved_calendar_cannot_be_changed_into_official_provenance(tmp_path):
    db,_=store(tmp_path)
    history=HistoryStore(tmp_path/'history.db')
    data=package()
    selection=dict(version=1,mode='DEMO',source='DEMO',through=data['available_through'],calendar=data['calendar'])
    path=tmp_path/'selection.json';path.write_text(json.dumps(selection),encoding='utf8')
    evaluate_selection(db,history,path,'DEMO',today=TODAY)
    with db.connect() as conn:
        before=conn.execute('SELECT COUNT(*) FROM recommendation_reviews').fetchone()[0]
        selection['calendar']['source']='OFFICIAL'
        conn.execute('UPDATE recommendation_selections SET payload=?',(json.dumps(selection),))
    with pytest.raises(ValueError): evaluate_saved(db,history,'DEMO',today=TODAY)
    with db.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM recommendation_reviews').fetchone()[0]==before


@pytest.mark.parametrize('stamp,version,count,kept', [
    ('2026-10-16T15:00:00+09:00',1,1,False),
    ('2026-10-17T10:00:00+09:00',0,1,False),
    ('2026-10-17T10:00:00+09:00',1,2,False),
    ('2026-10-17T10:00:00',1,1,False),
    ('2099-10-17T10:00:00+09:00',1,1,False),
    ('2026-10-17T10:00:00+09:00',1,1,True)])
def test_krx_cached_close_requires_next_day_observation(tmp_path,stamp,version,count,kept):
    history = HistoryStore(tmp_path/'history.db')
    history.insert_bars([Bar('2026-10-16','900001','시험','KOSPI',100,100,100,100,1000,100,4000)],'KRX')
    with history.connect() as conn:
        conn.execute('''INSERT INTO fetched_days(source,market,day,count,checked_at,normalizer_version)
            VALUES(?,?,?,?,?,?)''',('KRX','KOSPI','2026-10-16',count,stamp,version))
    assert bool(history.recommendation_bars('KRX','2026-10-16','2026-10-16')) is kept
