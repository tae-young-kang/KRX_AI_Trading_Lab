"""Theme 1 daily research detector. No network, notifications or orders."""
from datetime import date
from math import isfinite, fsum
from fractions import Fraction
import re
from app.recommendation_outcomes import session_day

RULES_TEXT=('테마1 · 종목을 찾는 조건\n'
    '대상: 코스피·코스닥 종목\n'
    '최근 6거래일 중 아래 두 조건을 같은 날 만족한 날이 3일 이상이면 감지해요.\n'
    '① 하루 거래대금 100억원 이상\n'
    '② 전일 종가 대비 당일 고가 +5% 이상\n'
    '고가 상승률 = (당일 고가 ÷ 전일 종가 − 1) × 100\n'
    '휴장일은 제외해요. 새 거래일이 들어오면 가장 오래된 거래일을 빼요.\n'
    '전일 종가 계산 때문에 판정에는 최소 7거래일의 일봉이 필요해요.\n'
    '조건을 만족한 날짜는 4일 이상이어도 모두 표시해요. 날짜·요일, 고가 상승률, 거래대금을 보여줘요.\n'
    '만족한 모든 날짜의 고가 상승률을 더해 65% 이상 / 65% 미만으로 표시해요.\n'
    '65%는 분류 기준이며, 합계가 미만이어도 위 조건에 맞으면 감지해요. 투자 수익률이 아니에요.\n'
    '종목명·시장·감지일 시가총액·NXT 거래 가능 여부도 함께 안내해요. 확인 근거가 없으면 미확인으로 표시해요.\n'
    '일반 추천 조건과 별도의 발굴 방법이에요. /조건변경으로 테마1 기준이 바뀌지는 않아요.')

SETUP_GUIDANCE=('\nPC 홈 → 테마1 · 종목 찾기 → 테마1 검색 시작을 누르세요.\n'
    '실제 종목: KRX 수집 후 판정 → 시작. 요청일 기준 거래일은 자동 계산해요.\n'
    'KRX OPEN API 인증키와 유가증권·코스닥 일별매매정보 서비스 승인이 필요해요. 키움 키와 달라요.\n'
    '확인한 일봉 CSV를 고급 연구에서 넣고 CSV_USER로 판정할 수도 있어요.\n'
    '완료 후 /테마1에서 결과를 봅니다. /테마1만 보내면 새 수집을 시작하지 않아요.\n'
    '조건 설명: /테마1조건 · 가짜 연습 자료는 실제 추천이 아니에요.')


def detect(*, code, market, bars, sessions, through, mode, price_source):
    """Require six confirmed sessions plus the preceding close, never weekday guesses."""
    if (market not in ('KOSPI','KOSDAQ') or mode not in ('DEMO','MOCK','REAL_QUOTES')
            or not isinstance(code,str) or not re.fullmatch(r'[A-Za-z0-9]{6}',code)):
        raise ValueError('시장과 자료 모드를 확인해 주세요.')
    if price_source not in ('DEMO','KRX','CSV_USER') or (mode=='DEMO')!=(price_source=='DEMO'):
        raise ValueError('가격 자료 출처를 확인해 주세요.')
    begin,end,days=sessions.validate(mode)
    target=session_day(through)
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
    evidence=result.get('nxt_evidence')
    if evidence:
        venue+=f'\nNXT 관측 시각: {evidence["observed_at"]} · 현재 주문 가능 상태를 보장하지 않아요.'
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
    quality=result.get('ingest_quality')
    if quality:
        lines.extend([f'전일 종가 포함 7거래일 수집 근거: 원본 수 미확인 {quality["unknown_days"]}일 · 정규화 제외 {quality["omitted_rows"]}행 · 관측/가격 행 수 불일치 {quality["stale_days"]}일',
                      '저장된 자료 범위의 감지입니다. 전체 종목 수집 완료는 미확인입니다.'])
    return '\n'.join(lines)
