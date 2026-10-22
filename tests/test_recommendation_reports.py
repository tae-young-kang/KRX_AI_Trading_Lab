"""Temporary stored evidence and Telegram-shaped updates; no real clients."""
from dataclasses import replace
from copy import deepcopy
import pytest
from app.storage import Database
from app.config import DEFAULTS
from app.recommendation_outcomes import SessionEvidence
from test_recommendations import candidates, save
from test_recommendation_outcomes import DAYS, prices
from test_telegram_orders import sandbox, update


def store(tmp_path):
    db = Database(tmp_path/'practice.db')
    sid = save(db,candidates(),deepcopy(DEFAULTS))
    # Pin recommendation time; do not depend on test machine's calendar.
    with db.connect() as record:
        record.execute('UPDATE recommendation_batches SET created_at=?',('2026-10-19T10:00:00+09:00',))
    evidence = dict(scan_id=sid,code='900001',mode='DEMO',bars=prices(),
        sessions=SessionEvidence(DAYS,DAYS[0],DAYS[-1],'DEMO'),available_through=DAYS[-1],price_source='DEMO')
    return db,evidence


def test_reviews_are_idempotent_and_corrections_append_without_rewriting_recommendation(tmp_path):
    db,evidence = store(tmp_path)
    db.review_recommendation(**evidence); db.review_recommendation(**evidence)
    before = db.recommendation_history('DEMO')
    changed = [replace(bar,close=bar.close+10) for bar in prices()]
    db.review_recommendation(**{**evidence,'bars':changed})
    assert db.recommendation_history('DEMO') == before
    with db.connect() as record:
        rows = record.execute('SELECT * FROM recommendation_reviews ORDER BY id').fetchall()
        assert len(rows)==2 and rows[0]['results_json'] != rows[1]['results_json']
    report = db.recommendation_summary('DEMO',performance=True)
    assert '1거래일' in report and '가상 자료' in report and '실제 수익률·예측이 아니에요' in report
    assert len(report)<4096


def test_reviews_are_scoped_and_missing_evidence_is_not_zero_return(tmp_path):
    db,evidence = store(tmp_path)
    assert '평가 자료 없음' in db.recommendation_summary('DEMO',performance=True)
    assert '저장된 추천이 없어요' in db.recommendation_summary('MOCK',performance=True)
    with pytest.raises(ValueError): db.review_recommendation(**{**evidence,'mode':'MOCK'})
    with db.connect() as record:
        assert record.execute('SELECT COUNT(*) FROM recommendation_reviews').fetchone()[0] == 0
    missing = [bar for bar in prices() if bar.date != DAYS[1]]
    db.review_recommendation(**{**evidence,'bars':missing})
    assert '가격 자료 없음' in db.recommendation_summary('DEMO',performance=True)


def test_report_commands_authenticated_readonly_and_bound_to_receiver_mode(tmp_path,sandbox):
    db,_ = store(tmp_path)
    service,broker,_,_ = sandbox
    calls=[]
    def report(performance):
        calls.append(performance)
        return db.recommendation_summary('DEMO',performance=performance)
    service.recommendation_report=report
    bad=update(sandbox,1,'/추천기록');bad['message']['from']['id']=99
    assert service.handle(bad) is None and calls==[]
    event=update(sandbox,2,'/추천기록')
    first=service.handle(event)
    assert service.handle(event)==first and calls==[False]
    assert '900001' in first and '실제 주문은 하지 않았어요' in first
    service.handle(update(sandbox,3,'/중지'))
    assert '평가 자료 없음' in service.handle(update(sandbox,4,'/추천성적'))
    assert broker.calls==[]
