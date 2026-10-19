"""User-selected cached closes and confirmed sessions; no market requests."""
from datetime import datetime,timedelta
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


def evaluate_recent(database,history,mode,*,source='KRX',today=None):
    """Explicit cached evaluation using XKRX sessions, without a date file.

    This does not fetch prices or certify the current day's close. Future
    sessions identify not-yet-due horizons; future prices are never loaded.
    Existing explicitly saved selections are not overwritten.
    """
    today=today or datetime.now(KST).date()
    if mode not in ('MOCK','REAL_QUOTES') or source not in ('KRX','CSV_USER'):
        raise ValueError('실제·모의 시세 모드에서 KRX 또는 직접 확인한 CSV 일봉을 선택해 주세요.')
    with database.connect() as db:
        rows=db.execute('''SELECT DISTINCT b.created_at FROM recommendation_batches b
            JOIN recommendations r ON r.scan_id=b.scan_id WHERE b.mode=?''',(mode,)).fetchall()
    dates=[]
    for row in rows:
        try:
            stamp=datetime.fromisoformat(row['created_at'])
            if stamp.tzinfo is None:raise ValueError()
            dates.append(stamp.astimezone(KST).date())
        except (ValueError,TypeError):
            raise ValueError('저장된 추천 시각을 확인해야 해요. 기존 평가는 바꾸지 않았어요.') from None
    dates=[day for day in dates if day<today]
    if not dates:raise ValueError('어제 이전의 추천 기록이 있어야 이후 가격을 확인할 수 있어요.')
    begin=min(dates)
    if (today-begin).days>3650:
        raise ValueError('10년을 넘는 추천 기록은 고급 화면에서 평가 기간을 나눠 주세요.')
    try:
        import exchange_calendars
        end=today+timedelta(days=60)
        calendar=exchange_calendars.get_calendar('XKRX',start=begin.isoformat(),end=end.isoformat())
        days=[session.date().isoformat() for session in calendar.sessions
              if begin<=session.date()<=end]
        through=max(day for day in days if day<today.isoformat())
    except Exception:
        raise ValueError('추천 평가 거래일을 확인하지 못했어요. 거래일 지원 범위를 확인해 주세요.') from None
    data=dict(version=1,mode=mode,source=source,through=through,
              calendar=dict(days=days,begin=begin.isoformat(),end=end.isoformat(),source='EXCHANGE_CALENDARS_XKRX'))
    data,sessions=parse_selection(json.dumps(data).encode('utf-8'),mode,today=today,minimum_sessions=1)
    return _evaluate(database,history,data,sessions,mode,today)


def _evaluate(database, history, data, sessions, mode, today):
    # Cache has no exchange final-close certification for the current day.
    if session_day(data['through']) >= today:
        raise ValueError('저장 시세 평가는 오늘 이전 완료일을 선택해 주세요. 당일 자료는 확정 종가인지 확인되지 않았어요.')
    bars = history.recommendation_bars(data['source'],sessions.begin,data['through'])
    return apply_prices(database,mode,sessions,bars,data['through'],data['source'])
