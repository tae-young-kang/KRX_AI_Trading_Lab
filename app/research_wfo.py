"""Fixed rolling research, not order execution, an index benchmark or a portfolio."""
from dataclasses import asdict
from datetime import datetime,date,timedelta
import csv
import hashlib
import json
from math import isfinite
from pathlib import Path
from statistics import mean
from app.research import build_features,make_variants,qualify,HORIZONS
from app.krx_history import validate_bars
from app.trading_clock import KST

METHOD='ROLLING_WFO_V1'
SETTINGS=dict(train_sessions=60,test_sessions=20,step_sessions=20,warmup=20,purge=5,min_folds=3,min_train_days=12,min_oos_days=5)
NOTICE='관측 수익 양수 비율은 비용 차감 뒤 양수였던 관측 비율이며 미관측/미산출은 분모에서 제외합니다. 실제 계좌 청산 승률과 다릅니다. 당일 일봉 종가로 조건 판단 → 다음 일봉 시가를 연구 진입가로 → 1/3/5거래일 종가로 평가합니다. 공식 첫 체결/가상 추적 체결 인증이 아니며 장중 익절·손절 체결을 재현하지 않습니다. 기간을 나눠 반복검증한 연구예요. 실제 매매·복리 계좌 수익이 아니며 겹친 보유기간은 서로 상관됩니다. 비교값은 같은 시장의 자료 있는 종목군 평균이지 KOSPI/KOSDAQ 지수가 아닙니다. 실제 원천·기업행동·생존편향·전체시장 및 미래 성과는 미검증입니다.'


def _json(value):return json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False,separators=(',',':'))


def market_sessions(begin,end):
    import exchange_calendars
    first=date.fromisoformat(begin);last=date.fromisoformat(end)
    cal=exchange_calendars.get_calendar('XKRX',start=(first-timedelta(days=7)).isoformat(),end=(last+timedelta(days=7)).isoformat())
    return [day.strftime('%Y-%m-%d') for day in cal.sessions_in_range(begin,end)]


def _mean(values):
    if not values:return None
    result=mean(values)
    if not isfinite(result):raise ValueError('연구 수치 계산 범위를 확인해 주세요.')
    return round(result,8)


def _stats(by_day,sessions,variant,days,*,topk,cost,cutoff=None,fold=None):
    index={day:i for i,day in enumerate(sessions)}
    daily={h:[] for h in HORIZONS};excess={h:[] for h in HORIZONS};wins={h:[] for h in HORIZONS}
    count={str(h):dict(selected=0,observed=0,missing=0,tail=0,purged=0) for h in HORIZONS};signals=[]
    for day in days:
        rows=by_day.get((variant['market'],day),[])
        selected=sorted((r for r in rows if qualify(r,variant)),key=lambda r:(r['score'],r['code']),reverse=True)[:topk]
        pool=[r for r in rows if r['close']>=variant['min_price'] and r['cap_eok'] is not None and r['value_eok'] is not None]
        allowed=cutoff is None or index[day]+SETTINGS['purge']<len(sessions) and sessions[index[day]+SETTINGS['purge']]<=cutoff
        values={h:[] for h in HORIZONS}
        for rank,row in enumerate(selected,1):
            signal=dict(strategy=variant['id'],split='WFO_TEST' if cutoff is None else 'WFO_TRAIN',day=day,code=row['code'],name=row['name'],market=variant['market'],rank=rank,score=row['score'],fold=fold)
            for h in HORIZONS:
                entry=count[str(h)];entry['selected']+=1;value=row['future'][h]
                if not allowed:entry['purged']+=1;value=None
                elif index[day]+h>=len(sessions):entry['tail']+=1;value=None
                elif value is None:entry['missing']+=1
                elif row['date_exit'][h]!=sessions[index[day]+h]:raise ValueError('연구 청산일이 확인 거래일과 달라요.')
                elif cutoff is not None and row['date_exit'][h]>cutoff:entry['purged']+=1;value=None
                else:
                    if not isfinite(value):raise ValueError('관측 가격 변화 범위를 확인해 주세요.')
                    entry['observed']+=1;values[h].append(value);wins[h].append(value-cost>0)
                signal['r'+str(h)]=None if value is None else round(value-cost,8)
            signals.append(signal)
        for h in HORIZONS:
            if not values[h]:continue
            basket=[r['future'][h] for r in pool if r['future'][h] is not None]
            if not basket:continue
            daily[h].append(mean(values[h])-cost);excess[h].append(mean(values[h])-mean(basket))
    metrics=dict(days3=len(daily[3]),coverage=count,selected=count['3']['selected'])
    for h in HORIZONS:
        metrics['net'+str(h)]=_mean(daily[h]);metrics['basket_excess'+str(h)]=_mean(excess[h]);metrics['win'+str(h)]=_mean([100*int(x) for x in wins[h]])
    return metrics,signals


def _rank(rows):
    return sorted(rows,key=lambda r:(r['train']['days3']>=SETTINGS['min_train_days'],r['train']['basket_excess3'] if r['train']['basket_excess3'] is not None else -1e100,r['train']['days3'],r['id']),reverse=True)


def evaluate_wfo(bars,*,source,coverage_quality=None,topk=5,now=None):
    if source not in ('KRX','CSV_USER','DEMO_SYNTHETIC') or type(topk) is not int or not 1<=topk<=50:raise ValueError('연구 자료 출처·추천 수를 확인해 주세요.')
    now=now or datetime.now(KST)
    if now.tzinfo is None or now.utcoffset() is None:raise ValueError('연구 확인 시각을 확인해 주세요.')
    raw=validate_bars(bars)
    rows=raw
    if not rows:raise ValueError('연구할 저장 일봉이 없어요.')
    today=now.astimezone(KST).date().isoformat()
    provisional=sorted({b.date for b in raw if source!='DEMO_SYNTHETIC' and b.date==today})
    past=[b for b in raw if b.date<today]
    only_provisional=source!='DEMO_SYNTHETIC' and not past and bool(provisional)
    if source!='DEMO_SYNTHETIC' and not any(b.date>today for b in raw) and past:rows=past
    rows=sorted(rows,key=lambda b:(b.market,b.date,b.code))
    quality=coverage_quality or dict(state='NOT_CHECKED')
    report=dict(method_version=METHOD,source=source,as_of=now.isoformat(),last_confirmed_day=max((b.date for b in past),default=None) if source!='DEMO_SYNTHETIC' else None,excluded_provisional_days=provisional,raw_data_fingerprint=hashlib.sha256(_json(dict(source=source,rows=[asdict(b) for b in sorted(raw,key=lambda b:(b.market,b.date,b.code))])).encode()).hexdigest(),source_notice='합성 연습 자료 · 실제 성과 미검증' if source=='DEMO_SYNTHETIC' else '사용자가 선택한 실제자료 출처 · 실제 성과/원천 검증 미완료',condition_count=48,settings=dict(SETTINGS,topk=topk,cost_roundtrip_pct=.25),
        data_begin=min(b.date for b in rows),data_end=max(b.date for b in rows),data_rows=len(rows),symbols=len({(b.market,b.code) for b in rows}),
        data_fingerprint=hashlib.sha256(_json(dict(source=source,rows=[asdict(b) for b in rows])).encode()).hexdigest(),coverage_quality=quality,markets={},folds=[],variants=[],top_signals=[],latest_candidates={},status='INSUFFICIENT_DATA',notice=NOTICE)
    calendars={m:sorted({b.date for b in rows if b.market==m}) for m in ('KOSPI','KOSDAQ')}
    blocked=False
    for market,days in calendars.items():
        issues=[];missing=[];unexpected=[];expected=days if source=='DEMO_SYNTHETIC' else []
        if not days:issues.append('시장 자료 없음')
        if only_provisional:issues.append('당일 잠정자료만 있음 · 확정 과거자료 없음')
        if days and any(d>today for d in days):issues.append('미래 날짜 자료')
        if source!='DEMO_SYNTHETIC' and days:
            try:
                expected=market_sessions(report['data_begin'],report['data_end'])
                missing=sorted(set(expected)-set(days));unexpected=sorted(set(days)-set(expected))
                if missing:issues.append('시장 전체 거래일 누락')
                if unexpected:issues.append('확인 거래일 밖 자료')
            except Exception:issues.append('XKRX 거래일 확인 실패')
        if quality.get('state') not in ('NOT_CHECKED','REFERENCE_FLOOR_MET'):issues.append('외부 자료 대조 경고/미확인')
        report['markets'][market]=dict(state='DATA_BLOCKED' if issues else 'INSUFFICIENT_DATA',issues=issues,missing_sessions=missing,unexpected_sessions=unexpected,sessions=len(days),calendar_source='DEMO_OBSERVED' if source=='DEMO_SYNTHETIC' else 'EXCHANGE_CALENDARS_XKRX',calendar_sessions=expected,calendar_fingerprint=hashlib.sha256(_json(expected).encode()).hexdigest(),valid_folds=0,remaining_sessions=max(0,140-len(days)),latest_train=None)
        if days and issues:blocked=True
    if blocked:
        report['status']='DATA_BLOCKED';return report
    by_day,feature_quality=build_features(rows,SETTINGS['warmup']);report['feature_quality']=feature_quality
    variants=make_variants()
    for market,days in calendars.items():
        info=report['markets'][market]
        if not days:continue
        options=[v for v in variants if v['market']==market]
        def training(train_days,cutoff):
            output=[]
            for v in options:
                stats,_=_stats(by_day,days,v,train_days,topk=topk,cost=.25,cutoff=cutoff)
                output.append(dict(id=v['id'],name=v['name'],market=market,params={k:x for k,x in v.items() if k not in ('id','name')},train=stats))
            ranked=_rank(output);chosen=next((r for r in ranked if r['train']['days3']>=12),None)
            return ranked,chosen
        for start in range(80,len(days)-19,20):
            cutoff=days[start-1];train_days=days[start-60:start];test_days=days[start:start+20]
            ranked,chosen=training(train_days,cutoff)
            fold=dict(market=market,number=(start-80)//20+1,train_begin=train_days[0],train_end=cutoff,test_begin=test_days[0],test_end=test_days[-1],train_variants=ranked,chosen_id=chosen['id'] if chosen else None,chosen_name=chosen['name'] if chosen else None,train=chosen['train'] if chosen else None,test=None,state='INSUFFICIENT_TRAIN')
            if chosen:
                v=next(v for v in options if v['id']==chosen['id'])
                stats,signals=_stats(by_day,days,v,test_days,topk=topk,cost=.25,fold=fold['number'])
                fold['test']=stats;fold['state']='VALID_RESEARCH' if stats['days3']>=5 else 'INSUFFICIENT_OOS'
                report['top_signals'].extend(signals)
                if fold['state']=='VALID_RESEARCH':info['valid_folds']+=1
            report['folds'].append(fold)
        if len(days)>=80:
            ranked,chosen=training(days[-60:],days[-1])
            info['latest_train']=dict(begin=days[-60],cutoff=days[-1],chosen_id=chosen['id'] if chosen else None)
            report['variants'].extend(ranked)
            if chosen:report['latest_candidates'][market]=chosen['id']
        info['state']='SUFFICIENT_RESEARCH_UNVERIFIED' if info['valid_folds']>=3 else 'INSUFFICIENT_DATA'
    if all(info['valid_folds']>=3 for info in report['markets'].values()):report['status']='RESEARCH_ONLY_UNVERIFIED'
    report['latest_candidates_notice']='최신 성숙 학습 후보는 과거 OOS 평가와 별개입니다. OOS가 좋은 조건을 다시 골라 적용하지 않습니다.'
    return report


def export_wfo(report,path):
    root=Path(path);root.mkdir(parents=True,exist_ok=True)
    for filename,items,fields in (
        ('summary.csv',report['variants'],['id','name','market','params','train']),
        ('folds.csv',report['folds'],['market','number','train_begin','train_end','test_begin','test_end','chosen_id','chosen_name','state','train','test']),
        ('signals.csv',report['top_signals'],['strategy','split','day','code','name','market','rank','score','fold','r1','r3','r5'])):
        with (root/filename).open('w',encoding='utf-8-sig',newline='') as stream:
            writer=csv.DictWriter(stream,fields,extrasaction='ignore');writer.writeheader()
            writer.writerows({k:_json(v) if isinstance(v,(dict,list)) else v for k,v in item.items()} for item in items)
    (root/'manifest.json').write_text(json.dumps(report,ensure_ascii=False,allow_nan=False,indent=2),encoding='utf-8')
    return root


def detail(report,variant):
    def fmt(x):return '미산출' if x is None else f'{x:+.3f}'
    market=variant['market'];stats=variant['train'];lines=[f'{variant["name"]} · 최신 학습 관측 {stats["days3"]}일',f'최신 학습 3일 비용포함 평균 {fmt(stats["net3"])}% · 종목군 평균 대비 {fmt(stats["basket_excess3"])}%p',report['latest_candidates_notice'],'아래는 시장별로 당시 학습에서 선택한 조건의 평가예요. 현재 행의 조건을 계속 적용한 성과가 아닙니다.']
    for fold in report['folds']:
        if fold['market']!=market:continue
        test=fold['test'];value='후보/관측 부족' if test is None else f'3일 평균 {fmt(test["net3"])}% · 종목군 대비 {fmt(test["basket_excess3"])}%p · 관측 {test["days3"]}일'
        lines.append(f'{fold["number"]}회: 학습 {fold["train_begin"]}~{fold["train_end"]} → 평가 {fold["test_begin"]}~{fold["test_end"]} · 당시 선택 {fold["chosen_name"] or "없음"} · {value}')
        if test is not None:
            for horizon in HORIZONS:
                coverage=test['coverage'][str(horizon)]
                lines.append(f'{horizon}일 평균 {fmt(test["net"+str(horizon)])}% · 관측 수익 양수 비율 {fmt(test["win"+str(horizon)])}% · 관측 {coverage["observed"]}/선정 {coverage["selected"]} · 누락 {coverage["missing"]} · 자료 끝 {coverage["tail"]}')
    lines.append(NOTICE);return '\n'.join(lines)
