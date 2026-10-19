"""Cross-sectional selection research. NOT a tradeable portfolio or proven AI.

Signals are formed AFTER the current daily close. All features only use current
or past bars. Labels enter NEXT session open and exit after 1/3/5 sessions.
No simulated intraday stops, NXT trading, real fills or compound wealth curves.
The last 5 observations near sample end are unlabeled, never assumed winners.
"""
from __future__ import annotations
import csv
import hashlib
import json
import math
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path
from statistics import mean

HORIZONS=(1,3,5)


def pct(x, y):
    return 100*(x/y-1) if y else None


def clamp(value,lo=0,hi=1):
    return min(hi,max(lo,value))


def ranks(values):
    """Average rank on ties (Spearman)."""
    order=sorted(range(len(values)),key=lambda i:values[i]);out=[0.0]*len(values)
    p=0
    while p<len(order):
        q=p+1
        while q<len(order) and values[order[p]]==values[order[q]]:
            q+=1
        r=(p+1+q)/2
        for k in order[p:q]:out[k]=r
        p=q
    return out


def spearman(x,y):
    if len(x)<3 or len(x)!=len(y):return None
    a=ranks(x);b=ranks(y);xa=mean(a);yb=mean(b)
    dx=sum((v-xa)**2 for v in a);dy=sum((v-yb)**2 for v in b)
    return sum((u-xa)*(v-yb) for u,v in zip(a,b))/math.sqrt(dx*dy) if dx and dy else None


def make_variants():
    """Predeclared factor conditions; no test-period tuning."""
    strategies=[]
    for market in ('KOSPI','KOSDAQ'):
        for vr in (120,150,200,300):
            for cap in (1000,3000,10000):
                for low in (0.0,2.0):
                    d={'market':market,'min_volume_ratio':vr,'min_cap_eok':cap,
                       'min_change_pct':low,'max_change_pct':10.0,
                       'min_value_eok':30.0,'min_price':1000.0,
                       'require_above_ma':True}
                    d['name']=f'{market} | 거래량≥{vr}%·시총≥{cap:,}억·상승≥{low:g}%'
                    d['id']=hashlib.sha1(json.dumps(d,ensure_ascii=False,sort_keys=True).encode()).hexdigest()[:12]
                    strategies.append(d)
    return strategies


def build_features(bars, window=20):
    by_code=defaultdict(list)
    for b in bars:
        by_code[(b.market,b.code)].append(b)
    by_day=defaultdict(list)
    trading_calendar=defaultdict(set)
    for b in bars: trading_calendar[b.market].add(b.date)
    market_dates={m:sorted(ds) for m,ds in trading_calendar.items()}
    market_dayindex={m:{d:i for i,d in enumerate(ds)} for m,ds in market_dates.items()}
    invalid=0
    for (market,code),arr in by_code.items():
        arr.sort(key=lambda b:b.date)
        for i in range(window,len(arr)):
            b=arr[i];prev=arr[i-window:i]
            session_index=market_dayindex[market][b.date]
            # Require the IMMEDIATELY preceding market sessions. A suspended
            # stock's last 20 recorded bars may span months and otherwise make
            # volume spikes/MA look comparable to continuously traded stocks.
            # Never bridge an absent day or manufacture an interpolated bar.
            if (session_index < window or
                    [bar.date for bar in prev] != market_dates[market][session_index-window:session_index]):
                invalid+=1
                continue
            if min(x.volume for x in prev)<=0:invalid+=1;continue
            vr=b.volume/(sum(x.volume for x in prev)/window)*100
            change=pct(b.close,arr[i-1].close)
            ma=sum(x.close for x in prev)/window
            if change is None:continue
            value=b.trade_value_eok   # Missing != zero or gross close*volume.
            cap=b.cap_eok
            vscore=clamp(math.log2(max(vr,1e-6))/3)
            lscore=clamp(math.log1p(max(value or 0,0))/math.log1p(1000))
            pscore=clamp(1-abs(change-3)/8)
            score=round(45*vscore+30*lscore+25*pscore,4)
            future={}
            for horizon in HORIZONS:
                # The next h-th session is defined by the MARKET calendar.
                # If a stock stopped trading, never shift its entry/exit later.
                aligned=(session_index+horizon<len(market_dates[market]) and
                         i+horizon<len(arr) and
                         arr[i+1].date==market_dates[market][session_index+1] and
                         arr[i+horizon].date==market_dates[market][session_index+horizon])
                if aligned:
                    entry=arr[i+1].open
                    end=arr[i+horizon].close
                    # avoid adjacent-day split distortions; if a corporate action caused
                    # a huge discontinuity, flag instead of trusting adjusted prices.
                    future[horizon]=pct(end,entry) if entry>0 else None
                else:future[horizon]=None
            row={'day':b.date,'market':market,'code':code,'name':b.name,
                 'score':score,'vol_ratio':vr,'cap_eok':cap,'value_eok':value,
                 'close':b.close,'change_pct':change,'above_ma':b.close>ma,
                 'future':future,'feature_index':i,'date_exit':{
                    h:arr[i+h].date if future[h] is not None else None for h in HORIZONS}}
            by_day[(market,b.date)].append(row)
    return by_day,{'symbols':len(by_code),'invalid_history':invalid}


def qualify(row,v):
    return (row['vol_ratio']>=v['min_volume_ratio'] and
        row['cap_eok'] is not None and row['cap_eok']>=v['min_cap_eok'] and
        row['value_eok'] is not None and row['value_eok']>=v['min_value_eok'] and
        row['close']>=v['min_price'] and
        v['min_change_pct']<=row['change_pct']<=v['max_change_pct'] and
        (not v['require_above_ma'] or row['above_ma']))


def mean_or_none(a):return mean(a) if a else None


def evaluate_research(bars,topk=5,train_fraction=0.70,cost_roundtrip_pct=0.25,min_train_days=12,minimum_history=20):
    from app.research_stability import split_test_stability
    if not 1<=topk<=50:raise ValueError('Top-K는 1~50만 지원합니다.')
    if not .5<=train_fraction<=.85:raise ValueError('학습 비율은 50~85%만 지원합니다.')
    if cost_roundtrip_pct<0 or cost_roundtrip_pct>=10:raise ValueError('비용 설정이 올바르지 않습니다.')
    if len(bars)<40:raise ValueError('이력이 부족합니다. 종목별 최소 20+5 거래일 이상이 필요합니다.')
    markets=defaultdict(set)
    for b in bars:markets[b.market].add(b.date)
    cuts={}
    for market,dates in markets.items():
        days=sorted(dates)
        cuts[market]=days[min(len(days)-1,int(len(days)*train_fraction)-1)]
    by_day,quality=build_features(bars,minimum_history)
    market_days={m:sorted(dd) for m,dd in markets.items()}
    # Chronological splits using an embargo: TRAIN labels must finish on/before
    # the training cutoff; TEST signals require NO training overlap.
    outcomes={};signals=[];rows_summary=[]
    for v in make_variants():
        market=v['market'];cut=cuts.get(market)
        if cut is None:continue
        for day in market_days[market]:
            arr=by_day.get((market,day),[])
            if not arr:continue
            eligible=[r for r in arr if qualify(r,v)]
            selected=sorted(eligible,key=lambda r:(r['score'],r['code']),reverse=True)[:topk]
            split='TRAIN' if day<=cut else 'TEST'
            for pos,r in enumerate(selected,1):
                # explicitly report unavailable future for later days, do not invent returns
                values={f'r{h}':round(r['future'][h]-cost_roundtrip_pct,5)
                        if r['future'][h] is not None and (split=='TEST' or (r['date_exit'][h] is not None and r['date_exit'][h]<=cut))
                        else None for h in HORIZONS}
                signals.append({'strategy':v['id'],'split':split,'day':day,'code':r['code'],
                                'name':r['name'],'market':market,'rank':pos,'score':r['score'],**values})
            if not selected:continue
            benchmark=[r for r in arr if r['future'][3] is not None and r['close']>=v['min_price']
                       and r['cap_eok'] is not None and r['value_eok'] is not None
                       and (split=='TEST' or (r['date_exit'][5] is not None and r['date_exit'][5]<=cut))]
            valid=[r for r in selected if r['future'][3] is not None and r['date_exit'][3] is not None]
            # TRAIN labels must finish within train period, preventing leakage.
            valid=[r for r in valid if split=='TEST' or r['date_exit'][5] is not None and r['date_exit'][5]<=cut]
            if not valid:continue
            bmean=mean_or_none([r['future'][3] for r in benchmark])
            if bmean is None:continue
            ic_subset=[r for r in eligible if r['future'][3] is not None and
                       (split=='TEST' or (r['date_exit'][3] is not None and r['date_exit'][3]<=cut))]
            rank_ic=spearman([r['score'] for r in ic_subset],[r['future'][3] for r in ic_subset])
            r1=mean_or_none([r['future'][1] for r in valid if r['future'][1] is not None])
            r3=mean([r['future'][3] for r in valid]);r5=mean_or_none([r['future'][5] for r in valid if r['future'][5] is not None])
            key=(v['id'],split)
            outcomes.setdefault(key,[]).append({'day':day,'n':len(valid),'r1':r1,'r3':r3,'r5':r5,
                    'net3':r3-cost_roundtrip_pct,'base3':bmean-cost_roundtrip_pct,
                    'excess3':r3-bmean,'rank_ic':rank_ic,
                    'wins':sum(r['future'][3]-cost_roundtrip_pct>0 for r in valid),
                    'losers':sum(r['future'][3]-cost_roundtrip_pct<=0 for r in valid)})
        summary={'id':v['id'],'name':v['name'],'market':market,'params':{k:v[k] for k in v if k not in ('id','name')}}
        for split in ('TRAIN','TEST'):
            d=outcomes.get((v['id'],split),[])
            prefix='train' if split=='TRAIN' else 'test'
            summary.update({f'{prefix}_days':len(d),f'{prefix}_signals':sum(x['n'] for x in d),
                f'{prefix}_return3':round(mean([x['net3'] for x in d]),4) if d else None,
                f'{prefix}_excess3':round(mean([x['excess3'] for x in d]),4) if d else None,
                f'{prefix}_ic':round(mean([x['rank_ic'] for x in d if x['rank_ic'] is not None]),4) if any(x['rank_ic'] is not None for x in d) else None,
                f'{prefix}_win_days_pct':round(100*sum(x['net3']>0 for x in d)/len(d),1) if d else None,
                f'{prefix}_signal_win_pct':round(100*sum(x['wins'] for x in d)/sum(x['wins']+x['losers'] for x in d),1) if d else None})
        # TEST diagnostic, split at the same *market-session* midpoint for all
        # variants. Never use it in the TRAIN-only ranking or candidate choice.
        sessions=[day for day in market_days[market] if day>cut]
        summary.update(split_test_stability(
            sessions, outcomes.get((v['id'],'TEST'), [])))
        rows_summary.append(summary)
    # Critical: choose by TRAIN only. TEST labels may be displayed but are
    # never used to rank/select/adjust candidates. Do not automatically apply.
    ranked=sorted(rows_summary,key=lambda x:(x['train_days']>=min_train_days,
            x['train_excess3'] if x['train_excess3'] is not None else -1e8,
            x['train_days']),reverse=True)
    qualified=[x for x in ranked if x['train_days']>=min_train_days]
    winner=next((x['id'] for x in qualified),None)
    if not qualified:
        status='INSUFFICIENT_DATA'
    else:
        status='RESEARCH_ONLY'
    # The latest signals are not live orders; no signal may have matured yet.
    return {'status':status,'train_fraction':train_fraction,
        'cuts':cuts,'data_begin':min(b.date for b in bars),'data_end':max(b.date for b in bars),
        'source_notice':'synthetic/live source tag is managed by caller',
        'symbols':quality['symbols'],'data_rows':len(bars),'invalid_history':quality['invalid_history'],
        'topk':topk,'cost_pct':cost_roundtrip_pct,'chosen_on_train_only':winner,
        'variants':ranked,'top_signals':signals,
        'method':'Daily close signal → next day OPEN entry → day+1/3/5 CLOSE. Equal-weight per signal day; NOT compounded. Avg daily excess vs same-market eligible stocks. No intraday execution/stop. Holdout exploratory only.',
        'stability_method':'TEST market sessions split into chronological early/late halves, identical date boundary for same-market variants. Per-day 3-session benchmark-relative excess averaged only over matured observations; no 0% fills, no selection/ranking by TEST. Repeated adjacent holding periods and same market are correlated.'}


def export_report(report,path):
    from app.research_comparison import compare_variants
    root=Path(path);root.mkdir(parents=True,exist_ok=True)
    with (root/'summary.csv').open('w',encoding='utf-8-sig',newline='') as f:
        fields=['id','name','market','train_days','train_signals','train_return3','train_excess3','train_ic','train_win_days_pct',
                'test_days','test_signals','test_return3','test_excess3','test_ic','test_win_days_pct',
                'holdout_gap3','data_quality_label','comparison_note','adequate_train_days','adequate_test_days',
                'test_early_days','test_late_days','test_early_excess3','test_late_excess3',
                'test_late_gap3','test_stability_note']
        wr=csv.DictWriter(f,fields,extrasaction='ignore');wr.writeheader();wr.writerows(compare_variants(report))
    with (root/'signals.csv').open('w',encoding='utf-8-sig',newline='') as f:
        fields=['strategy','split','day','code','name','market','rank','score','r1','r3','r5']
        wr=csv.DictWriter(f,fields);wr.writeheader();wr.writerows(report['top_signals'])
    compact={k:v for k,v in report.items() if k not in ('variants','top_signals')}
    (root/'manifest.json').write_text(json.dumps(compact,ensure_ascii=False,indent=2),encoding='utf-8')
    return root
