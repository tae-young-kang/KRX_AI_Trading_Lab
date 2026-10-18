"""User-selected cached closes and confirmed sessions; no market requests."""
from datetime import datetime
import json
from app.theme_input import load_selection, parse_selection
from app.recommendation_import import apply_prices
from app.recommendation_outcomes import session_day
from app.trading_clock import KST


def evaluate_selection(database, history, path, mode, *, today=None):
    today = today or datetime.now(KST).date()
    data, sessions = load_selection(path,mode,today=today,minimum_sessions=1)
    result=_evaluate(database,history,data,sessions,mode,today)
    with database.connect() as db:
        db.execute('''INSERT INTO recommendation_selections VALUES(?,?,?) ON CONFLICT(mode)
            DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at''',
            (mode,json.dumps(data,ensure_ascii=False,sort_keys=True),datetime.now(KST).isoformat(timespec='seconds')))
    return result


def evaluate_saved(database, history, mode, *, today=None):
    today=today or datetime.now(KST).date()
    with database.connect() as db:
        row=db.execute('SELECT payload FROM recommendation_selections WHERE mode=?',(mode,)).fetchone()
    if not row:
        raise ValueError('먼저 이 모드에서 거래일 선택 파일로 평가해 주세요.')
    data,sessions=parse_selection(row['payload'].encode('utf-8'),mode,today=today,minimum_sessions=1)
    return _evaluate(database,history,data,sessions,mode,today)


def _evaluate(database, history, data, sessions, mode, today):
    # Cache has no exchange final-close certification for the current day.
    if session_day(data['through']) >= today:
        raise ValueError('저장 시세 평가는 오늘 이전 완료일을 선택해 주세요. 당일 자료는 확정 종가인지 확인되지 않았어요.')
    bars = history.recommendation_bars(data['source'],sessions.begin,data['through'])
    return apply_prices(database,mode,sessions,bars,data['through'],data['source'])
