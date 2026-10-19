"""Request-date XKRX calendar. Library calendar, never claimed as official evidence."""
from datetime import datetime,timedelta
from functools import lru_cache
from app.trading_clock import KST
from app.theme_input import parse_selection
import json


@lru_cache(maxsize=16)
def recent_sessions(day):
    try:
        import exchange_calendars
        start=day-timedelta(days=90)
        calendar=exchange_calendars.get_calendar('XKRX',start=start.isoformat(),end=day.isoformat())
        days=[d.strftime('%Y-%m-%d') for d in calendar.sessions[-7:]]
        if len(days)!=7:raise ValueError()
        return tuple(days)
    except Exception:
        raise ValueError('거래일 달력을 준비하지 못했어요. 프로그램 의존성 설치와 거래일 달력 지원 연도를 확인해 주세요. 날짜를 평일로 추측하지 않았어요.') from None


def request_selection(mode,source='KRX',*,now=None):
    moment=now or datetime.now(KST)
    if not isinstance(moment,datetime) or moment.tzinfo is None:
        raise ValueError('요청 시각의 한국 날짜를 확인해야 해요.')
    day=moment.astimezone(KST).date()
    days=recent_sessions(day)
    data=dict(version=1,mode=mode,source=source,through=days[-1],
        calendar=dict(days=list(days),begin=days[0],end=days[-1],
                      source='DEMO' if mode=='DEMO' else 'EXCHANGE_CALENDARS_XKRX'))
    return parse_selection(json.dumps(data).encode(),mode,today=day)[0]
