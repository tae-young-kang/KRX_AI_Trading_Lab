from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
import pytest
from app.krx_history import HistoryStore
from app.recommendation_schedule import run_due,summary,calendar_source
from app.storage import Database
from app.config import DEFAULTS

NOW=datetime(2026,10,19,12,tzinfo=timezone.utc)  # 21:00 KST


def test_automatic_calendar_advances_after_restart_without_rewriting_recommendations(tmp_path):
    from app.backtest import Bar
    from test_recommendation_reports import store
    db,_=store(tmp_path);history=HistoryStore(tmp_path/'history.db')
    with db.connect() as conn:
        conn.execute("UPDATE recommendation_batches SET mode='REAL_QUOTES',created_at='2026-10-08T10:00:00+09:00'")
    days=['2026-10-08','2026-10-12','2026-10-13','2026-10-14','2026-10-15','2026-10-16','2026-10-19']
    history.insert_bars([Bar(d,'900001','시험','KOSPI',100,110,90,100,1000,100,4000) for d in days],'CSV_USER')
    before=db.recommendation_history('REAL_QUOTES')
    first=run_due(db,history,'REAL_QUOTES',enabled=True,now=NOW.replace(day=13),automatic_source='CSV_USER')
    assert first['state']=='SUCCEEDED' and first['result']['available_through']=='2026-10-12'
    reopened=Database(db.path)
    later=run_due(reopened,history,'REAL_QUOTES',enabled=True,now=NOW.replace(day=20),automatic_source='CSV_USER')
    assert later['state']=='SUCCEEDED' and later['result']['available_through']=='2026-10-19'
    assert reopened.recommendation_history('REAL_QUOTES')==before
    text=summary(reopened,'REAL_QUOTES',enabled=True,now=NOW.replace(day=20),automatic_source='CSV_USER')
    assert '거래일 자동 계산' in text and '자동 연장하지 않아요' not in text
    assert calendar_source({'recommendation_calendar_sources':{'REAL_QUOTES':'CSV_USER'}},'MOCK') is None
    assert calendar_source({'recommendation_calendar_sources':{'REAL_QUOTES':'CSV_USER'}},'REAL_QUOTES')=='CSV_USER'
    assert calendar_source({'recommendation_calendar_sources':[]},'REAL_QUOTES') is None
    assert DEFAULTS['recommendation_calendar_sources']=={}
    with pytest.raises(ValueError):
        run_due(reopened,history,'DEMO',enabled=True,now=NOW,automatic_source='CSV_USER')


def test_opt_in_clock_boundary_and_persistent_single_daily_attempt(tmp_path,monkeypatch):
    db=Database(tmp_path/'practice.db');history=HistoryStore(tmp_path/'history.db')
    calls=[]
    def evaluate(*args,**kwargs):
        calls.append(kwargs['today'])
        return dict(available_through='2026-10-16')
    monkeypatch.setattr('app.recommendation_schedule.evaluate_saved',evaluate)
    assert DEFAULTS['recommendation_auto_evaluate'] is False
    assert run_due(db,history,'DEMO',enabled=False,now=NOW)['state']=='DISABLED'
    assert run_due(db,history,'DEMO',enabled=True,now=NOW.replace(hour=11,minute=29))['state']=='NOT_DUE'
    def run(_): return run_due(db,history,'DEMO',enabled=True,now=NOW.replace(hour=11,minute=30))
    with ThreadPoolExecutor(max_workers=4) as pool: results=list(pool.map(run,range(4)))
    assert sum(row['state']=='SUCCEEDED' for row in results)==1 and len(calls)==1
    reopened=Database(db.path)
    assert run_due(reopened,history,'DEMO',enabled=True,now=NOW)['state']=='ALREADY_ATTEMPTED'
    assert run_due(reopened,history,'MOCK',enabled=True,now=NOW)['state']=='SUCCEEDED'


def test_failure_and_interrupted_attempt_are_not_retried(tmp_path):
    db=Database(tmp_path/'practice.db');history=HistoryStore(tmp_path/'history.db')
    assert run_due(db,history,'MOCK',enabled=True,now=NOW)['state']=='FAILED'
    assert run_due(db,history,'MOCK',enabled=True,now=NOW)['previous']=='FAILED'
    with db.connect() as conn:
        conn.execute("INSERT INTO recommendation_evaluation_jobs VALUES('DEMO','2026-10-19','RUNNING',?,NULL)",(NOW.isoformat(),))
    assert run_due(db,history,'DEMO',enabled=True,now=NOW)['previous']=='RUNNING'
    with pytest.raises(ValueError): run_due(db,history,'DEMO',enabled=True,now=NOW.replace(tzinfo=None))


def test_summary_is_mode_scoped_read_only_and_does_not_echo_corruption(tmp_path):
    db=Database(tmp_path/'practice.db')
    text=summary(db,'DEMO',enabled=False,now=NOW)
    assert '자동 갱신: 꺼짐' in text and '거래일 설정 없음' in text and '시도 기록 없음' in text
    with db.connect() as conn:
        conn.execute("INSERT INTO recommendation_evaluation_jobs VALUES('MOCK','2026-10-19','FAILED',?,NULL)",(NOW.isoformat(),))
        conn.execute("INSERT INTO recommendation_selections VALUES('MOCK',?,?)",('PRIVATE_INVALID_JSON',NOW.isoformat()))
        before=tuple(conn.iterdump())
    text=summary(db,'MOCK',enabled=True,now=NOW)
    assert '설정 확인 필요' in text and '평가 미완료' in text and 'PRIVATE' not in text
    assert '평가 미완료' not in summary(db,'DEMO',enabled=True,now=NOW)
    with db.connect() as conn: assert tuple(conn.iterdump())==before
