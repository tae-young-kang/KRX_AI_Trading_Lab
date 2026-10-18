from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
import pytest
from app.krx_history import HistoryStore
from app.recommendation_schedule import run_due
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
