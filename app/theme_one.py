"""Theme 1 daily research detector. No network, notifications or orders."""
from datetime import date
from math import isfinite, fsum
from fractions import Fraction
import re


def detect(*, code, market, bars, sessions, through, mode, price_source):
    """Require six confirmed sessions plus the preceding close, never weekday guesses."""
    if (market not in ('KOSPI','KOSDAQ') or mode not in ('DEMO','MOCK','REAL_QUOTES')
            or not isinstance(code,str) or not re.fullmatch(r'[A-Za-z0-9]{6}',code)):
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
    window_days={d.isoformat() for d in window}
    for bar in bars:
        if bar.code!=code or bar.market!=market or bar.date not in window_days:
            continue
        if bar.date in indexed:
            raise ValueError('같은 종목·날짜의 자료가 중복됐어요.')
        indexed[bar.date]=bar
    if any(day.isoformat() not in indexed for day in window):
        return dict(status='PRICE_MISSING',matched=False,hits=[])
    hits=[]
    exact_sum=Fraction(0)
    for previous,day in zip(window,window[1:]):
        bar=indexed[day.isoformat()]
        close=indexed[previous.isoformat()].close
        values=(close,bar.open,bar.high,bar.low,bar.close,bar.trade_value_eok)
        if (not all(type(v) in (int,float) and isfinite(v) for v in values)
                or min(values[:5])<=0 or bar.trade_value_eok<0
                or not isinstance(bar.name,str) or not 1<=len(bar.name)<=100
                or bar.high<max(bar.open,bar.close) or bar.low>min(bar.open,bar.close)):
            raise ValueError('종가·고가·거래대금 자료를 확인해 주세요.')
        # Value filter first; prices have already arrived in a daily bulk dataset.
        if bar.trade_value_eok<100:
            continue
        exact_pct=(Fraction(str(bar.high))/Fraction(str(close))-1)*100
        try:
            high_pct=float(exact_pct)
        except OverflowError:
            raise ValueError('고가 등락률 계산 범위를 벗어났어요.') from None
        if not isfinite(high_pct):
            raise ValueError('고가 등락률 계산 범위를 벗어났어요.')
        if exact_pct>=5:
            hits.append(dict(day=bar.date,high_pct=high_pct,trade_value_eok=bar.trade_value_eok))
            exact_sum+=exact_pct
    today=indexed[through]
    cap=today.cap_eok
    if type(cap) not in (int,float) or not isfinite(cap) or cap<=0:
        cap=None
    try:
        high_sum=float(exact_sum)
        value_sum=fsum(h['trade_value_eok'] for h in hits)
        if not isfinite(high_sum) or not isfinite(value_sum):
            raise ValueError()
    except (ValueError,OverflowError):
        raise ValueError('테마1 합계 계산 범위를 벗어났어요.') from None
    return dict(status='COMPLETE',matched=len(hits)>=3,code=code,name=today.name,market=market,
                detected_on=through,window=[d.isoformat() for d in window[1:]],hits=hits,
                reference_close=today.close,
                high_sum=high_sum,trade_value_sum_eok=value_sum,sum_band='ABOVE' if exact_sum>65 else 'EQUAL' if exact_sum==65 else 'BELOW',cap_eok=cap,
                price_source=price_source,calendar_source=sessions.source)


def alert_text(result, *, nxt_eligible=None, nxt_asof=None, nxt_source=None):
    """One stock per message; unknown/stale eligibility never means KRX only."""
    if result.get('status')!='COMPLETE' or not result.get('matched'):
        raise ValueError('자료가 완전하고 테마1에 감지된 종목만 알림으로 만들 수 있어요.')
    weekdays='월화수목금토일'
    def day_label(value):
        day=date.fromisoformat(value)
        return f'{value}({weekdays[day.weekday()]})'
    venue='NXT 거래 가능 여부 미확인'
    if type(nxt_eligible) is bool and nxt_asof==result['detected_on'] and isinstance(nxt_source,str) and nxt_source.strip():
        venue=('NXT 거래 가능' if nxt_eligible else 'KRX 전용 (NXT 대상 아님)')+f' · 근거: {nxt_source}'
    cap=result['cap_eok']
    lines=['[테마1 · 종목 감지]',f'{result["name"]} ({result["code"]}) · '+{'KOSPI':'코스피','KOSDAQ':'코스닥'}[result['market']],
           '감지 기준일: '+day_label(result['detected_on']),
           f'감지일 시가총액: {cap:,.2f}억 원' if cap is not None else '감지일 시가총액: 미확인',venue,
           f'최근 6거래일: {result["window"][0]} ~ {result["window"][-1]}',
           f'100억 원 이상 + 전일 종가 대비 고가 5% 이상: {len(result["hits"])}일']
    for index,hit in enumerate(result['hits'],1):
        lines.append(f'{index}. {day_label(hit["day"])} · 고가 +{hit["high_pct"]:.2f}% · 거래대금 {hit["trade_value_eok"]:,.2f}억 원')
    band={'ABOVE':'65% 초과','EQUAL':'65%와 같음','BELOW':'65% 미만'}[result['sum_band']]
    lines.extend([f'만족일 전체 고가 상승률 합계: {result["high_sum"]:.2f}% · {band}',
                  f'만족일 거래대금 합계: {fsum(h["trade_value_eok"] for h in result["hits"]):,.2f}억 원',
                  '고가 상승률 합계는 투자 수익률이 아닙니다. 주문은 보내지 않았어요.',
                  f'가격 출처: {result["price_source"]} · 거래일 근거: {result["calendar_source"]}'])
    return '\n'.join(lines)
