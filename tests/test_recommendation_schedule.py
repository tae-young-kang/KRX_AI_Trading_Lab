from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
import pytest
from app.krx_history import HistoryStore
from app.recommendation_schedule import run_due,summary
from app.storage import Database
from app.config import DEFAULTS

NOW=datetime(2026,10,19,12,tzinfo=timezone.utc)  # 21:00 KST


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
