"""Read-only, post-hoc KRX-open cohort/day-bar observations.

Daily OHLC never certifies an opening fill or which threshold traded first.
No paper entry, exit, broker call, portfolio PnL, or DB write is performed.
"""
from datetime import date
from decimal import Decimal
from math import isfinite
from pathlib import Path

from app.backtest import Bar
from app.morning_quote_router import _plan_identity, _safe_path
from app.morning_venue_policy import KRX_OPEN, stored_policy
from app.morning_virtual import MorningVirtual, aware, record_path
from app.storage import Database
from app.trading_clock import KST

SOURCES = {'DEMO': ('DEMO_SYNTHETIC',),
           'MOCK': ('KRX', 'CSV_USER'),
           'REAL_QUOTES': ('KRX', 'CSV_USER')}

NOTES = {
    'ENTRY_DAY_MISSING': '고정일의 일봉 시가가 없어요',
    'REVIEW_DAY_MISSING': '평가일의 일봉이 없어요',
    'NO_VALID_TRADES': '거래량이나 가격 근거가 부족해요',
    'BOTH_TOUCHED_ORDER_UNKNOWN': '익절·손절 가격대 모두 포함 · 선후 순서 미확인',
    'TAKE_RANGE_ONLY': '익절 가격대 포함 · 체결 여부 미확인',
    'STOP_RANGE_ONLY': '손절 가격대 포함 · 체결 여부 미확인',
    'NEITHER_TOUCHED': '평가일 고가·저가상 기준가격 미도달',
}


def _date(value):
    if not isinstance(value, str):
        raise ValueError('가상 일봉 관측 날짜를 확인해 주세요.')
    try:
        day = date.fromisoformat(value)
        if value != day.isoformat():
            raise ValueError()
    except ValueError:
        raise ValueError('가상 일봉 관측 날짜를 확인해 주세요.') from None
    return day


def _index(bars, *, entry_day, review_day):
    indexed = {}
    for item in bars:
        if type(item) is not Bar or item.date not in (entry_day, review_day):
            raise ValueError('일봉의 종목·날짜·자료 출처를 확인해 주세요.')
        if item.market not in ('KOSPI','KOSDAQ') or not isinstance(item.code,str):
            raise ValueError('일봉의 시장과 코드를 확인해 주세요.')
        nums = (item.open, item.high, item.low, item.close, item.volume)
        if (any(type(x) not in (int,float) or not isfinite(x) for x in nums)
                or min(nums[:4])<=0 or item.volume<0
                or item.low>item.high or item.low>min(item.open,item.close)
                or item.high<max(item.open,item.close)):
            raise ValueError('일봉의 가격·거래량 범위를 확인해 주세요.')
        key=(item.date,item.market,item.code)
        if key in indexed:
            raise ValueError('같은 날짜·시장·종목의 일봉이 중복됐어요.')
        indexed[key]=item
    return indexed


def observe(plan, bars, *, review_day, source):
    """Observe individual OHLC ranges, never infer executed stop/take fills."""
    start = _date(plan['day'])
    end = _date(review_day)
    if end < start:
        raise ValueError('고정 추천일 이후의 일봉을 확인해 주세요.')
    if source not in SOURCES.get(plan.get('mode'), ()):
        raise ValueError('가상 목록의 시세 모드와 일봉 출처가 달라요.')
    if stored_policy(plan) != KRX_OPEN:
        raise ValueError('기존 NXT/KRX 혼합 목록은 KRX 일봉 시가로 평가할 수 없어요.')
    _plan_identity(plan,plan['mode'],plan['day'])
    rows = _index(bars, entry_day=plan['day'],review_day=review_day)
    cfg=plan['settings']
    take=Decimal(str(cfg['exit_take_pct']))/100
    stop=Decimal(str(cfg['exit_stop_pct']))/100
    results=[]
    for target in plan['targets']:
        code=target['code']
        key=(target['market'],code)
        entry=rows.get((plan['day'],*key))
        review=rows.get((review_day,*key))
        item={'code':code,'name':target['name'],'market':target['market'],
              'status':None,'change_pct':None}
        if entry is None:
            item['status']='ENTRY_DAY_MISSING'
        elif review is None:
            item['status']='REVIEW_DAY_MISSING'
        elif entry.volume==0 or review.volume==0:
            item['status']='NO_VALID_TRADES'
        else:
            open_price=Decimal(str(entry.open))
            high=Decimal(str(review.high))
            low=Decimal(str(review.low))
            hit_take=high>=open_price*(1+take)
            hit_stop=low<=open_price*(1-stop)
            item['status']=('BOTH_TOUCHED_ORDER_UNKNOWN' if hit_take and hit_stop
                            else 'TAKE_RANGE_ONLY' if hit_take
                            else 'STOP_RANGE_ONLY' if hit_stop
                            else 'NEITHER_TOUCHED')
            item['change_pct']=round(float((Decimal(str(review.close))/open_price-1)*100),3)
        results.append(item)
    return dict(cohort_day=plan['day'],review_day=review_day,mode=plan['mode'],
                source=source,results=results)


def report_saved(root, mode, cohort_day, review_day, *, history, source, now, page=1):
    """Display post-hoc evidence only; do not initialize/mutate any portfolio."""
    from app.morning_virtual import MorningVirtual
    if not aware(now) or type(page) is not int or not 1<=page<=999999:
        raise ValueError('평가 시각과 페이지를 확인해 주세요.')
    if (_date(cohort_day)>_date(review_day)
            or _date(review_day)>=now.astimezone(KST).date()):
        raise ValueError('고정일 이후, 확인이 끝난 과거 날짜만 평가해요.')
    if source not in SOURCES.get(mode, ()):
        raise ValueError('모드와 자료 출처가 일치하지 않아요.')
    root=Path(root)
    path=record_path(root,mode,cohort_day)
    _safe_path(root,path)
    if not path.is_file():
        return '고정한 아침 추천 목록이 없어요. 일봉 가격으로 목록을 소급 생성하지 않아요.'
    reader=MorningVirtual(Database(path,read_only=True))
    plan=reader.plan()
    if plan['day']!=cohort_day or plan['mode']!=mode:
        raise ValueError('고정 목록의 날짜·모드가 달라요.')
    # KRX daily bars are accepted only through recommendation_bars, which
    # rejects same-day/unconfirmed snapshots. No fallback to raw intraday bars.
    first=history.recommendation_bars(source,cohort_day,cohort_day)
    last=(first if cohort_day==review_day else
          history.recommendation_bars(source,review_day,review_day))
    report=observe(plan,first+([] if cohort_day==review_day else last),
                   review_day=review_day,source=source)
    targets=report['results']
    pages=max(1,(len(targets)+4)//5)
    if page>pages:
        return f'사후 일봉 관측은 {pages}페이지까지 있어요.'
    lines=[f'[추천 일봉 사후 관측] 목록 {cohort_day} · 관측 {review_day} · {page}/{pages}페이지',
           f'{mode} · 자료 {source} · 고정 종목 {len(targets)}개',
           '고정일의 일봉 시가 대비 종가 변화와 평가일의 고가·저가 범위만 비교해요.',
           '일봉 시가는 공식 첫 체결/주문 체결을 증명하지 않으며, 매수·매도·승패·실현손익을 기록하지 않아요.']
    for item in targets[(page-1)*5:page*5]:
        pct='미확인' if item['change_pct'] is None else f"{item['change_pct']:+.3f}%"
        lines.append(f"{item['name']} ({item['code']}): 종가 변화 {pct} · {NOTES[item['status']]}")
    lines.append('다른 거래일에 먼저 청산됐는지는 알 수 없어요. 상장·분할·배당·수정주가도 별도 대조가 필요해요.')
    if page>1:lines.append(f'이전: /가상일봉 {cohort_day} {review_day} {source} {page-1}')
    if page<pages:lines.append(f'다음: /가상일봉 {cohort_day} {review_day} {source} {page+1}')
    return '\n'.join(lines)
