import json
from app.backtest import Bar
from app.krx_history import HistoryStore
from app.recommendation_outcomes import SessionEvidence
from app.theme_store import prepare
from app.theme_budget import preview_latest,preview_text


def test_budget_uses_frozen_run_close_not_mutated_cache(tmp_path):
    history=HistoryStore(tmp_path/'hist.db')
    days=('2026-10-08','2026-10-12','2026-10-13','2026-10-14','2026-10-15','2026-10-16','2026-10-19')
    history.insert_bars([Bar(d,'900001','연습','KOSPI',100,110,99,100,1000,100,4000) for d in days],'DEMO')
    prepare(history,sessions=SessionEvidence(days,days[0],days[-1],'DEMO'),through=days[-1],mode='DEMO',source='DEMO')
    with history.connect() as db: db.execute('UPDATE daily_bars SET close=105')
    result=preview_latest(history,mode='DEMO',total_won=1000,fee_bps=0,buffer_bps=0)
    assert result['targets'][0]['reference_won']==100 and result['targets'][0]['quantity']==10
    assert result['executable'] is False and result['state']=='FUNDS_UNKNOWN'
    text=preview_text(result)
    assert '현재가·시초가가 아닙니다' in text and '주문 안 함' in text
    with history.connect() as db:
        row=db.execute('SELECT id,result_json FROM theme_alerts').fetchone()
        payload=json.loads(row['result_json']);payload['result'].pop('reference_close')
        db.execute('UPDATE theme_alerts SET result_json=? WHERE id=?',(json.dumps(payload),row['id']))
    legacy=preview_latest(history,mode='DEMO',total_won=1000,fee_bps=0,buffer_bps=0)
    assert legacy['targets'][0]['quantity'] is None and legacy['unreserved_won']==1000
