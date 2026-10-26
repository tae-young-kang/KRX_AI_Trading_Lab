"""Bounded recovery of missing KRX market-days using an injected quote client."""
from datetime import datetime
import json
from app.recommendation_outcomes import session_day
from app.recommendation_cache import evaluate_requested
from app.theme_input import parse_selection
from app.trading_clock import KST


def missing_plan(database,mode,through,*,today,limit=60):
    if mode not in ('MOCK','REAL_QUOTES') or type(limit) is not int or not 1<=limit<=60:
        raise ValueError('가격 보완의 모드·최대 조회 수를 확인해 주세요.')
    if session_day(through)>=today:raise ValueError('오늘 이전의 완료일만 보완할 수 있어요.')
    with database.connect() as db:
        rows=db.execute('''SELECT v.results_json,r.market FROM recommendation_reviews v
            JOIN recommendation_batches b ON b.scan_id=v.scan_id
            JOIN recommendations r ON r.scan_id=v.scan_id AND r.code=v.code
            WHERE b.mode=? AND v.price_source='KRX' AND v.available_through=?
            AND v.id=(SELECT MAX(x.id) FROM recommendation_reviews x
                WHERE x.scan_id=v.scan_id AND x.code=v.code AND x.price_source='KRX')''',(mode,through)).fetchall()
    targets=set();missing=0
    try:
        for row in rows:
            if row['market'] not in ('KOSPI','KOSDAQ'):raise ValueError()
            for item in json.loads(row['results_json']):
                if item['state']!='PRICE_MISSING':continue
                day=session_day(item['target_day'])
                if day>=today or day>session_day(through):raise ValueError()
                targets.add((row['market'],day.isoformat()));missing+=1
    except (TypeError,ValueError,KeyError):
        raise ValueError('저장된 평가의 시장·누락 날짜를 확인해야 해요.') from None
    ordered=sorted(targets,key=lambda item:(item[1],item[0]))
    return dict(targets=ordered[:limit],remaining_targets=max(0,len(ordered)-limit),missing_observations=missing)


def collect_missing(database,history,client,mode,*,source=None,now=None,today=None,progress=None,cancelled=None):
    moment=now or datetime.now(KST)
    if not isinstance(moment,datetime) or moment.tzinfo is None:
        raise ValueError('자료 보완 시각의 시간대를 확인해 주세요.')
    today=today or moment.astimezone(KST).date()
    if today>moment.astimezone(KST).date() or mode not in ('MOCK','REAL_QUOTES') or source not in (None,'KRX'):
        raise ValueError('가격 보완은 실제·모의 시세의 저장 KRX 일봉만 지원해요.')
    if source is None:
        with database.connect() as db:
            row=db.execute('SELECT payload FROM recommendation_selections WHERE mode=?',(mode,)).fetchone()
        if row:
            data,_=parse_selection(row['payload'].encode(),mode,today=today,minimum_sessions=1)
            if data['source']!='KRX':raise ValueError('직접 넣은 CSV 자료는 자동 수집으로 바꾸지 않아요. KRX 자료를 선택해 주세요.')
    def stopped():
        if cancelled and cancelled():raise ValueError('자료 보완을 중지했어요. 이미 저장한 일봉은 남아 있어요.')
    stopped()
    before=evaluate_requested(database,history,mode,source=source,today=today)
    plan=missing_plan(database,mode,before['available_through'],today=today)
    requests=rows=0
    for index,(market,day) in enumerate(plan['targets'],1):
        stopped()
        if progress:progress(f'성적 자료 보완 {index}/{len(plan["targets"])} · {market} {day}')
        fetched=history.fetch_range(client,day,day,markets=(market,),now=moment)
        requests+=fetched['requests'];rows+=fetched['rows']
    stopped()
    result=evaluate_requested(database,history,mode,source=source,today=today)
    result['collection']=dict(requests=requests,rows=rows,remaining_targets=plan['remaining_targets'])
    return result
