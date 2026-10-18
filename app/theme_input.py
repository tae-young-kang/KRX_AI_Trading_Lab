"""Explicit local calendar selection for cached daily theme research."""
from datetime import date, datetime
import json
from app.recommendation_outcomes import SessionEvidence, session_day
from app.theme_store import prepare
from app.trading_clock import KST


def load_selection(path, mode, *, today=None, minimum_sessions=7):
    with open(path,'rb') as stream:
        raw=stream.read(100001)
    try:
        if len(raw)>100000:
            raise ValueError()
        data=json.loads(raw.decode('utf-8-sig'))
        if (not isinstance(data,dict) or set(data)!={'version','mode','source','through','calendar'}
                or type(data['version']) is not int or data['version']!=1
                or mode not in ('DEMO','MOCK','REAL_QUOTES') or data['mode']!=mode):
            raise ValueError()
        calendar=data['calendar']
        if (not isinstance(calendar,dict) or set(calendar)!={'days','begin','end','source'}
                or not isinstance(calendar['days'],list) or len(calendar['days'])>10000
                or not all(isinstance(day,str) for day in calendar['days'])):
            raise ValueError()
        if mode=='DEMO':
            if data['source']!='DEMO' or calendar['source']!='DEMO':
                raise ValueError()
        elif data['source'] not in ('KRX','CSV_USER') or calendar['source']!='USER_CONFIRMED':
            raise ValueError()
        sessions=SessionEvidence(tuple(calendar['days']),calendar['begin'],calendar['end'],calendar['source'])
        _,_,days=sessions.validate(mode)
        target=session_day(data['through'])
        if target not in days or target>(today or datetime.now(KST).date()) or sum(day<=target for day in days)<minimum_sessions:
            raise ValueError()
        return data,sessions
    except (ValueError,TypeError,KeyError,UnicodeError,OverflowError):
        raise ValueError(f'자료 선택 파일의 모드·출처·기준일·{minimum_sessions}거래일 이상 근거를 확인해 주세요. 공식 거래일 표시는 직접 등록할 수 없어요.') from None


def prepare_selection(history,path,mode,*,today=None):
    data,sessions=load_selection(path,mode,today=today)
    return prepare(history,sessions=sessions,through=data['through'],mode=mode,source=data['source'])
