"""Theme 1 daily research detector. No network, notifications or orders."""
from datetime import date
from math import isfinite, fsum


def detect(*, code, market, bars, sessions, through, mode, price_source):
    """Require six confirmed sessions plus the preceding close, never weekday guesses."""
    if market not in ('KOSPI','KOSDAQ') or mode not in ('DEMO','MOCK','REAL_QUOTES'):
        raise ValueError('시장과 자료 모드를 확인해 주세요.')
    if price_source not in ('DEMO','KRX','CSV_USER') or (mode=='DEMO')!=(price_source=='DEMO'):
        raise ValueError('가격 자료 출처를 확인해 주세요.')
    begin,end,days=sessions.validate(mode)
    target=date.fromisoformat(through)
    if not begin<=target<=end or target not in days:
        raise ValueError('감지 기준일은 확인된 거래일이어야 해요.')
    window=[day for day in days if day<=target][-7:]
    if len(window)<7:
        return dict(status='CALENDAR_MISSING',matched=False,hits=[])
    indexed={}
    for bar in bars:
        if bar.code!=code or bar.market!=market or bar.date not in {d.isoformat() for d in window}:
            continue
        if bar.date in indexed:
            raise ValueError('같은 종목·날짜의 자료가 중복됐어요.')
        indexed[bar.date]=bar
    if any(day.isoformat() not in indexed for day in window):
        return dict(status='PRICE_MISSING',matched=False,hits=[])
    hits=[]
    for previous,day in zip(window,window[1:]):
        bar=indexed[day.isoformat()]
        close=indexed[previous.isoformat()].close
        values=(close,bar.open,bar.high,bar.low,bar.close,bar.trade_value_eok)
        if (not all(type(v) in (int,float) and isfinite(v) for v in values)
                or min(values[:5])<=0 or bar.trade_value_eok<0
                or bar.high<max(bar.open,bar.close) or bar.low>min(bar.open,bar.close)):
            raise ValueError('종가·고가·거래대금 자료를 확인해 주세요.')
        # Value filter first; prices have already arrived in a daily bulk dataset.
        if bar.trade_value_eok<100:
            continue
        high_pct=(bar.high/close-1)*100
        if not isfinite(high_pct):
            raise ValueError('고가 등락률 계산 범위를 벗어났어요.')
        if bar.high>=close*1.05:
            hits.append(dict(day=bar.date,high_pct=high_pct,trade_value_eok=bar.trade_value_eok))
    today=indexed[through]
    cap=today.cap_eok
    if type(cap) not in (int,float) or not isfinite(cap) or cap<=0:
        cap=None
    return dict(status='COMPLETE',matched=len(hits)>=3,code=code,name=today.name,market=market,
                detected_on=through,window=[d.isoformat() for d in window[1:]],hits=hits,
                high_sum=fsum(h['high_pct'] for h in hits),cap_eok=cap,
                price_source=price_source,calendar_source=sessions.source)
