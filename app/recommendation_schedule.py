"""Opt-in daily cached evaluation, called by the open PC application only."""
from datetime import datetime,time
from app.recommendation_cache import evaluate_saved
from app.trading_clock import KST


def run_due(database,history,mode,*,enabled,now):
    if enabled is not True: return dict(state='DISABLED')
    if not isinstance(now,datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('시간대가 포함된 실행 시각이 필요해요.')
    if mode not in ('DEMO','MOCK','REAL_QUOTES'): raise ValueError('평가 모드를 확인해 주세요.')
    local=now.astimezone(KST)
    if local.time()<time(20,30): return dict(state='NOT_DUE')
    day=local.date().isoformat()
    with database.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        prior=db.execute('SELECT state FROM recommendation_evaluation_jobs WHERE mode=? AND day=?',(mode,day)).fetchone()
        if prior: return dict(state='ALREADY_ATTEMPTED',previous=prior['state'])
        db.execute('INSERT INTO recommendation_evaluation_jobs VALUES(?,?,?,?,NULL)',(mode,day,'RUNNING',local.isoformat()))
    try:
        result=evaluate_saved(database,history,mode,today=local.date())
        state='SUCCEEDED'
    except Exception:
        result=None
        state='FAILED'
    with database.connect() as db:
        db.execute('UPDATE recommendation_evaluation_jobs SET state=?,through_day=? WHERE mode=? AND day=?',
                   (state,result['available_through'] if result else None,mode,day))
    return dict(state=state,result=result)
