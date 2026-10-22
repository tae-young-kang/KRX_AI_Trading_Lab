"""Explicit Theme1 daily collection pipeline, with an injected read-only client."""
import json
from datetime import datetime

from app.theme_input import parse_selection, prepare_confirmed
from app.trading_clock import KST


def collect_and_prepare(history,client,data,mode,*,now=None,progress=None,cancelled=None):
    moment=now or datetime.now(KST)
    if not isinstance(moment,datetime) or moment.tzinfo is None:
        raise ValueError('한국 날짜를 확인할 수 있는 시각이 필요해요.')
    data,sessions=parse_selection(json.dumps(data).encode('utf-8'),mode,
                                  today=moment.astimezone(KST).date())
    if mode not in ('MOCK','REAL_QUOTES') or data['source']!='KRX':
        raise ValueError('KRX 수집은 키움 모의 시세/실제 시세 모드의 KRX 자료에만 사용할 수 있어요.')
    days=[d.isoformat() for d in sessions.validate(mode)[2] if d.isoformat()<=data['through']][-7:]
    if any(datetime.fromisoformat(day).weekday()>=5 for day in days):
        raise ValueError('KRX 수집 대상에 주말이 있어요. 확인한 거래일을 다시 살펴보세요.')
    total=requests=0
    for index,day in enumerate(days,1):
        if cancelled and cancelled():
            raise ValueError('테마1 수집을 중단했어요. 일부 일봉은 저장될 수 있으나 새 판정은 만들지 않았어요.')
        if progress:progress(f'테마1 일봉 {index}/7 · {day} · 코스피/코스닥')
        batch=history.fetch_range(client,day,day,now=moment)
        total+=batch['rows']
        requests+=batch['requests']
    if cancelled and cancelled():
        raise ValueError('테마1 수집을 중단했어요. 저장 시세는 보존하며 새 판정은 만들지 않았어요.')
    if progress:progress('테마1 · 저장 일봉 판정 중')
    result=prepare_confirmed(history,data,mode,today=moment.astimezone(KST).date())
    return dict(result,collection=dict(rows=total,requests=requests,days=len(days),
                                      provisional=data['through']==moment.astimezone(KST).date().isoformat()))
