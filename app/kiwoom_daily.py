"""Injected ka10081 KRX daily reader. No credentials, orders or DB mutation."""
from datetime import datetime
from collections.abc import Mapping
import re

from app.backtest import Bar
from app.krx_history import validate_bars
from app.recommendation_outcomes import session_day


def integer(value,*,price=False):
    if isinstance(value,str):value=value.strip()
    if not isinstance(value,str) or not re.fullmatch(r'[+-]?(?:[0-9]{1,20}|[0-9]{1,3}(?:,[0-9]{3}){1,6})',value):
        raise ValueError('키움 일봉의 가격·거래량·거래대금을 확인해야 해요.')
    number=int(value.replace(',',''))
    if price:
        number=abs(number)  # Kiwoom price signs describe direction, not negative prices.
        if number==0:raise ValueError('키움 일봉 가격이 없어요.')
    elif number<0:
        raise ValueError('키움 일봉 수량·거래대금이 음수예요.')
    return number


def read_daily(client,*,code,name,market,days,cancelled=None,max_pages=5):
    """Read unadjusted KRX bars for the requested sessions only; report missing days.

    `client` implements KiwoomMarket.query. No NXT/SOR suffix, market-cap inference,
    current-day finalization or conversion to an official KRX bulk source.
    """
    if (not isinstance(code,str) or not re.fullmatch(r'[A-Za-z0-9]{6}',code)
            or market not in ('KOSPI','KOSDAQ') or not isinstance(name,str) or not 1<=len(name)<=100
            or type(max_pages) is not int or not 1<=max_pages<=10
            or not isinstance(days,(list,tuple)) or not 1<=len(days)<=7):
        raise ValueError('키움 일봉의 종목과 요청 거래일을 확인해 주세요.')
    parsed=tuple(session_day(day) for day in days)
    if parsed!=tuple(sorted(set(parsed))):
        raise ValueError('키움 일봉 요청 거래일이 중복됐거나 순서가 달라요.')
    target=set(days)
    through=days[-1]
    body=dict(stk_cd=code,base_dt=through.replace('-',''),upd_stkpc_tp='0')
    found={}
    seen_dates=set()
    seen_keys=set()
    previous_day=None
    cont,next_key='N',''
    calls=0
    for _ in range(max_pages):
        if cancelled and cancelled():raise ValueError('키움 일봉 조회를 중단했어요.')
        data,headers=client.query('ka10081','/api/dostk/chart',body,
                                  cont,next_key,True,cancelled)
        calls+=1
        if (not isinstance(data,dict) or data.get('stk_cd')!=code or not isinstance(headers,Mapping)
                or str(data.get('return_code'))!='0'):
            raise ValueError('키움 일봉의 응답 종목·처리 결과가 요청과 달라요.')
        rows=data.get('stk_dt_pole_chart_qry')
        if not isinstance(rows,list) or len(rows)>10000:
            raise ValueError('키움 일봉 응답 목록을 확인해 주세요.')
        for row in rows:
            try:
                raw=row['dt'].strip() if isinstance(row['dt'],str) else row['dt']
                if not isinstance(raw,str) or not re.fullmatch(r'[0-9]{8}',raw):raise ValueError()
                day=datetime.strptime(raw,'%Y%m%d').date().isoformat()
                if day<days[0]:break
                if day>through or day in seen_dates or (previous_day and day>=previous_day):raise ValueError()
                seen_dates.add(day)
                previous_day=day
                if day not in target:continue
                bar=Bar(day,code,name,market,integer(row['open_pric'],price=True),
                        integer(row['high_pric'],price=True),integer(row['low_pric'],price=True),
                        integer(row['cur_prc'],price=True),integer(row['trde_qty']),
                        integer(row['trde_prica'])/100,None)
                validate_bars([bar])
                found[day]=bar
                if set(found)==target:break
            except (KeyError,TypeError,ValueError,OverflowError):
                raise ValueError('키움 일봉의 날짜·중복·가격·거래대금을 확인해야 해요. 자료를 저장하지 않았어요.') from None
        if set(found)==target or (previous_day and previous_day<=days[0]):break
        if headers.get('cont-yn','N')!='Y':break
        next_key=headers.get('next-key','')
        if not isinstance(next_key,str) or not next_key or next_key in seen_keys or not rows:
            raise ValueError('키움 일봉 연속조회가 반복되거나 끊겼어요.')
        seen_keys.add(next_key)
        cont='Y'
    return dict(bars=[found[d] for d in days if d in found],
                missing_days=[d for d in days if d not in found],api_calls=calls,
                venue='KRX',adjusted=False,source='KIWOOM_KRX_DAILY')
