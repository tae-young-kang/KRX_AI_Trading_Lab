"""Explicit new KRX opening policy; absent policy preserves legacy cohorts."""
from datetime import date,datetime,time,timedelta
from functools import lru_cache
from app.trading_clock import KST

KRX_OPEN='KRX_OPEN'


def validate(policy):
    if policy is not None and policy!=KRX_OPEN:raise ValueError('지원하지 않는 가상 매수시장 정책이에요.')
    return policy


def stored_policy(plan):
    version=plan.get('version')
    if type(version) is int and version==1 and 'venue_policy' not in plan:return None
    if type(version) is int and version==2 and plan.get('venue_policy')==KRX_OPEN:return KRX_OPEN
    raise ValueError('가상 목록의 버전·시장 정책을 확인해 주세요.')


@lru_cache(maxsize=128)
def krx_open(day):
    try:
        target=date.fromisoformat(day)
        if target.isoformat()!=day:raise ValueError()
        import exchange_calendars
        calendar=exchange_calendars.get_calendar('XKRX',start=(target-timedelta(days=7)).isoformat(),end=(target+timedelta(days=7)).isoformat())
        moment=calendar.session_open(day).to_pydatetime()
        if moment.tzinfo is None or moment.utcoffset() is None or moment.astimezone(KST).date()!=target:raise ValueError()
        return moment.astimezone(KST)
    except Exception:
        raise ValueError('KRX 거래일의 개장 시각을 확인하지 못했어요. 9시나 평일로 추측하지 않았어요.') from None


def deadline(day,policy):
    validate(policy)
    return krx_open(day) if policy==KRX_OPEN else datetime.combine(date.fromisoformat(day),time(8),KST)


def target_venue(target,policy):
    validate(policy)
    eligible=target.get('nxt_eligible')
    if eligible is not None and type(eligible) is not bool:raise ValueError('NXT 자격 자료를 확인해 주세요.')
    if policy==KRX_OPEN:
        if 'venue' in target and target['venue']!='KRX':raise ValueError('KRX 고정 정책과 종목 시장이 달라요.')
        return 'KRX'
    derived='NXT' if eligible is True else 'KRX' if eligible is False else 'UNKNOWN'
    if 'venue' in target:
        venue=target['venue']
        if venue not in ('NXT','KRX','UNKNOWN') or ('nxt_eligible' in target and venue!=derived):
            raise ValueError('기존 고정 시장과 자격 자료가 달라요.')
        return venue
    return derived


@lru_cache(maxsize=128)
def session_bounds(day):
    """Calendar-confirmed regular session; closing-auction timestamp inclusive."""
    if not isinstance(day,str):raise ValueError('고정 날짜를 확인해 주세요.')
    target=date.fromisoformat(day)
    if target.isoformat()!=day:raise ValueError('고정 날짜를 확인해 주세요.')
    try:
        import exchange_calendars
        calendar=exchange_calendars.get_calendar('XKRX',start=(target-timedelta(days=7)).isoformat(),end=(target+timedelta(days=7)).isoformat())
        opened=calendar.session_open(day).to_pydatetime()
        closed=calendar.session_close(day).to_pydatetime()
        if (opened.tzinfo is None or opened.utcoffset() is None or closed.tzinfo is None or closed.utcoffset() is None or opened>=closed
                or opened.astimezone(KST).date().isoformat()!=day or closed.astimezone(KST).date().isoformat()!=day):raise ValueError()
        return opened,closed
    except Exception:
        raise ValueError('해당 거래일의 정규장 시작·종료 시각을 확인하지 못했어요.') from None


def quote_session_open(moment,venue):
    """Quote evidence only. Order gates and NXT windows remain unchanged."""
    if moment.tzinfo is None or moment.utcoffset() is None:raise ValueError('확인된 시각이 필요해요.')
    day=moment.astimezone(KST).date()
    if venue=='KRX':
        opened,closed=session_bounds(day.isoformat())
        return opened<=moment<=closed
    from app.trading_clock import TradingCalendar
    return TradingCalendar(open_dates={day}).is_open(moment,venue)
