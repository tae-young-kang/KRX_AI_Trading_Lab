"""Explicit read-only Kiwoom Theme1 collection and isolated result persistence."""
from dataclasses import replace
from datetime import datetime
from math import isfinite

from app.theme_calendar import request_selection
from app.theme_sources import KIWOOM_SOURCES
from app.theme_candidates import plan_candidates
from app.kiwoom_daily import read_daily
from app.krx_history import validate_bars
from app.theme_input import prepare_confirmed
from app.nxt_evidence import record as record_nxt
from app.trading_clock import KST


def collect_theme(history,client,mode,*,now=None,selection=None,progress=None,cancelled=None,include_etf=False):
    moment=now or datetime.now(KST)
    if not isinstance(moment,datetime) or moment.tzinfo is None:
        raise ValueError('요청 시각의 한국 날짜를 확인해야 해요.')
    expected_url={'MOCK':'https://mockapi.kiwoom.com','REAL_QUOTES':'https://api.kiwoom.com'}
    if mode not in expected_url or client.url!=expected_url[mode] or type(include_etf) is not bool:
        raise ValueError('키움 일봉의 시세 모드와 서버가 달라요.')
    source=KIWOOM_SOURCES[mode]
    data=selection or request_selection(mode,source,now=moment)
    from app.theme_input import parse_selection
    import json
    data,_=parse_selection(json.dumps(data).encode(),mode,today=moment.astimezone(KST).date())
    if data['source']!=source:raise ValueError('키움 일봉 출처를 확인해 주세요.')
    days=[d for d in data['calendar']['days'] if d<=data['through']][-7:]
    def stopped():
        if cancelled and cancelled():raise ValueError('테마1 검색을 중단했어요. 새 판정은 저장하지 않았어요.')
    stopped()
    stocks=client.scan('전체',progress=progress,cancelled=cancelled,include_etf=include_etf)
    catalog={(s.market,s.code):s for s in stocks}
    if len(catalog)!=len(stocks) or not stocks:raise ValueError('전체 시장 종목 목록이 비었거나 중복됐어요.')
    baseline=history.load_bars('KRX',days[0],days[-2])
    plan=plan_candidates([(s.market,s.code,s.name) for s in stocks],baseline,days[:-1])
    buffered=[]
    missing=invalid=0
    for index,(market,code,name) in enumerate(plan['candidates'],1):
        stopped()
        if progress:progress(f'테마1 일봉 확인 {index}/{len(plan["candidates"])} · {name}')
        try:
            result=read_daily(client,code=code,name=name,market=market,days=days,cancelled=cancelled)
        except ValueError:
            stopped()
            missing+=1
            invalid+=1
            if progress:progress(f'{name} · 일봉 확인 필요, 다른 종목을 계속 확인합니다.')
            continue
        missing+=bool(result['missing_days'])
        stock=catalog[(market,code)]
        cap=stock.cap_eok
        if (not isinstance(stock.quote_stamp,str) or stock.quote_stamp[:10]!=data['through']
                or type(cap) not in (int,float) or not isfinite(cap) or cap<=0):cap=None
        buffered.extend(replace(b,cap_eok=cap if b.date==data['through'] else None) for b in result['bars'])
    stopped()
    validate_bars(buffered)
    # Replace this isolated source/window in one transaction. A prior candidate
    # excluded on this run cannot reappear from a previous partial cache.
    stamp=moment.isoformat(timespec='seconds')
    with history.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('DELETE FROM daily_bars WHERE source=? AND day BETWEEN ? AND ?', (source,days[0],days[-1]))
        db.executemany('''INSERT INTO daily_bars
            (source,day,market,code,name,open,high,low,close,volume,trade_value_eok,cap_eok,fetched_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',[
            (source,b.date,b.market,b.code,b.name,b.open,b.high,b.low,b.close,b.volume,b.trade_value_eok,b.cap_eok,stamp)
            for b in buffered])
    record_nxt(history,mode,stocks,now=moment)
    scope=dict(universe_count=plan['universe_count'],candidate_count=len(plan['candidates']),
               excluded_count=len(plan['excluded']),unknown_count=len(plan['unknown_history']),missing_symbols=missing,
               markets=sorted({m for m,_,_ in plan['candidates']}))
    scope['include_etf']=include_etf
    scope['invalid_symbols']=invalid
    return prepare_confirmed(history,data,mode,today=moment.astimezone(KST).date(),scope=scope)
