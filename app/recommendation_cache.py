"""User-selected cached closes and confirmed sessions; no market requests."""
from datetime import datetime
from app.theme_input import load_selection
from app.recommendation_import import apply_prices
from app.recommendation_outcomes import session_day
from app.trading_clock import KST


def evaluate_selection(database, history, path, mode, *, today=None):
    today = today or datetime.now(KST).date()
    data, sessions = load_selection(path,mode,today=today,minimum_sessions=1)
    # Cache has no exchange final-close certification for the current day.
    if session_day(data['through']) >= today:
        raise ValueError('저장 시세 평가는 오늘 이전 완료일을 선택해 주세요. 당일 자료는 확정 종가인지 확인되지 않았어요.')
    bars = history.recommendation_bars(data['source'],sessions.begin,data['through'])
    return apply_prices(database,mode,sessions,bars,data['through'],data['source'])
