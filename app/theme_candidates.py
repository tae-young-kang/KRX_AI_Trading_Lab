"""Conservative Theme1 lookup pruning from the five already elapsed sessions.

This is a query plan, never a recommendation or evidence for today's price.
"""
from fractions import Fraction
from math import isfinite
from app.krx_history import validate_bars
from app.recommendation_outcomes import session_day


def plan_candidates(universe,bars,days):
    """Universe is (market,code,name); days include baseline + five past sessions.

    Today can add at most one qualifying day. Exclude only when all five prior
    results are known and fewer than two qualify. Any missing price/value keeps
    the stock in the lookup plan. Neither today's turnover nor today's rise is
    an admissible universe filter: a stock may already have three past hits.
    """
    if not isinstance(days,(list,tuple)) or len(days)!=6:
        raise ValueError('이전 5거래일과 전일 종가용 1일이 필요해요.')
    parsed=tuple(session_day(d) for d in days)
    if parsed!=tuple(sorted(set(parsed))):raise ValueError('이전 거래일 순서를 확인해 주세요.')
    records=validate_bars(bars)
    indexed={(b.market,b.code,b.date):b for b in records}
    selected=[]
    excluded=[]
    unknown=[]
    seen=set()
    for item in universe:
        if not isinstance(item,(tuple,list)) or len(item)!=3:
            raise ValueError('전체 대상 종목 목록을 확인해 주세요.')
        market,code,name=item
        if (market not in ('KOSPI','KOSDAQ') or not isinstance(code,str) or len(code)!=6
                or not code.isascii() or not code.isalnum() or not isinstance(name,str) or not name
                or (market,code) in seen):
            raise ValueError('전체 대상 종목 목록의 시장·코드·중복을 확인해 주세요.')
        seen.add((market,code))
        rows=[indexed.get((market,code,d)) for d in days]
        uncertain=any(b is None for b in rows)
        hits=0
        if not uncertain:
            for previous,current in zip(rows,rows[1:]):
                value=current.trade_value_eok
                if type(value) not in (int,float) or not isfinite(value):
                    uncertain=True
                    continue
                if value>=100 and (Fraction(str(current.high))/Fraction(str(previous.close))-1)*100>=5:
                    hits+=1
        if uncertain:
            unknown.append((market,code))
        if uncertain or hits>=2:
            selected.append(tuple(item))
        else:
            excluded.append((market,code))
    return dict(universe_count=len(seen),candidates=selected,excluded=excluded,
                unknown_history=unknown,rule='PRIOR_FIVE_HITS_AT_LEAST_TWO_OR_UNKNOWN')
