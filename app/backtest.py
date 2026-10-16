"""Conservative, reproducible daily OHLCV research backtester.

CSV input is supplied BY THE USER and never described as live/exchange fills.
One-signal/day, next-session open entry, same-session stop/take (stop first on tie),
long-only. No NXT 08:00, intraday VWAP or order-book simulation.
"""
from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from statistics import mean


@dataclass(frozen=True)
class Bar:
    date: str
    code: str
    name: str
    market: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    trade_value_eok: float | None = None
    cap_eok: float | None = None


ALIASES = {
    'date': ('date','날짜','일자','일시','datetime'),
    'code': ('code','symbol','ticker','종목코드','종목번호'),
    'name': ('name','종목명'),
    'market': ('market','시장'),
    'open': ('open','시가'),
    'high': ('high','고가'),
    'low': ('low','저가'),
    'close': ('close','종가','현재가'),
    'volume': ('volume','거래량'),
    'trade_value_eok': ('trade_value_eok','거래대금(억)','거래대금_억원','거래대금_억'),
    'cap_eok': ('cap_eok','시가총액(억)','시가총액_억','시총(억)'),
}


def _number(v):
    if v is None or str(v).strip() == '':
        return None
    try:
        a = float(str(v).strip().replace(',', ''))
        return a if math.isfinite(a) else None
    except ValueError:
        return None


def load_ohlcv_csv(path):
    """Read one/multiple symbols. Requires true non-adjusted OHLC for execution.

    User is responsible for split/corporate-action adjustments, market calendars,
    data licensing and currency. This loader never downloads or invents prices.
    """
    rows = []
    with open(path, 'r', encoding='utf-8-sig', newline='') as f:
        rd = csv.DictReader(f)
        if not rd.fieldnames:
            raise ValueError('CSV에 헤더가 없습니다.')
        names = {str(c).strip().lower(): c for c in rd.fieldnames}
        columns = {}
        for key, aliases in ALIASES.items():
            columns[key] = next((names[a.lower()] for a in aliases if a.lower() in names), None)
        essential = ('date','code','open','high','low','close','volume')
        if any(columns[k] is None for k in essential):
            raise ValueError('필수 CSV 헤더: date,code,open,high,low,close,volume (또는 한글명)')
        seen = set()
        for line, raw in enumerate(rd, 2):
            def get(k):
                return raw.get(columns[k], '') if columns[k] else ''
            day_text = str(get('date')).strip().replace('.', '-').replace('/', '-')
            if len(day_text)==8 and day_text.isdigit():
                day_text = f'{day_text[:4]}-{day_text[4:6]}-{day_text[6:]}'
            try:
                day = datetime.strptime(day_text[:10], '%Y-%m-%d').date().isoformat()
            except ValueError as e:
                raise ValueError(f'{line}행 잘못된 날짜: {day_text}') from e
            code = str(get('code')).strip().lstrip("'")
            if not code:
                raise ValueError(f'{line}행 종목코드 누락')
            if code.isdigit():
                code = code.zfill(6)
            key = (code, day)
            if key in seen:
                raise ValueError(f'{line}행 중복 종목-일자: {code} {day}')
            seen.add(key)
            vals = {x:_number(get(x)) for x in ('open','high','low','close','volume',
                                                   'trade_value_eok','cap_eok')}
            if any(vals[k] is None for k in ('open','high','low','close','volume')):
                raise ValueError(f'{line}행 필수 시세값 누락 또는 비정상')
            o,h,l,c,v = (vals[k] for k in ('open','high','low','close','volume'))
            if min(o,h,l,c)<=0 or v<0 or l>min(o,c) or h<max(o,c) or l>h:
                raise ValueError(f'{line}행 OHLCV 범위 오류')
            rows.append(Bar(day,code,str(get('name')).strip() or code,
                        str(get('market')).strip().upper() or 'KOSPI',
                        o,h,l,c,v,vals['trade_value_eok'],vals['cap_eok']))
    if not rows:
        raise ValueError('데이터 행이 없습니다.')
    return sorted(rows, key=lambda x:(x.date,x.code))


def variants_from_config(cfg):
    """Precommitted experiment grid. Strategy tweaks require NEW run ID."""
    baseline = dict(cfg)
    baseline.update(backtest_volume_ratio=200.0, backtest_gap_limit=3.0,
                    backtest_entry_delay=1, exit_stop_pct=2.0, exit_take_pct=4.0,
                    exit_max_days=3)
    variants = [('기준선 · 거래량 200%',baseline,'BASELINE')]
    for threshold in (100,120,150,200,300):
        c=dict(baseline)
        c['backtest_volume_ratio']=float(threshold)
        variants.append((f'거래량 {threshold}%',c,'VOLUME'))
    for gap in (1.0,5.0):
        c=dict(baseline); c['backtest_gap_limit']=gap
        variants.append((f'갭상승 상한 +{gap:g}%',c,'ENTRY'))
    for delay in (2,):
        c=dict(baseline); c['backtest_entry_delay']=delay
        variants.append((f'매수 2번째 거래일 시가',c,'ENTRY'))
    for stop,take in ((1.5,3.0),(2.0,5.0),(3.0,6.0)):
        c=dict(baseline); c['exit_stop_pct']=stop; c['exit_take_pct']=take
        variants.append((f'손절 -{stop:g}% / 익절 +{take:g}%',c,'EXIT'))
    # Explicit non-overlapping markets to check regime effects.
    for mrkt in ('KOSPI','KOSDAQ'):
        c=dict(baseline); c['market']=mrkt
        variants.append((f'{mrkt} 단독',c,'MARKET'))
    return variants


def _metric_summary(trades, initial_cash, daily_equity):
    wins = [t for t in trades if t['pnl'] > 0]
    gross_gain = sum(max(0,t['pnl']) for t in trades)
    gross_loss = -sum(min(0,t['pnl']) for t in trades)
    peak=initial_cash; mdd=0
    for d in daily_equity:
        peak=max(peak,d['equity'])
        mdd=min(mdd,100*(d['equity']/peak-1))
    last=daily_equity[-1]['equity'] if daily_equity else initial_cash
    return {'closed':len(trades),'wins':len(wins),
            'winrate':100*len(wins)/len(trades) if trades else None,
            'realized_pnl':sum(t['pnl'] for t in trades),
            'equity':last,'return_pct':100*(last/initial_cash-1),
            'profit_factor':gross_gain/gross_loss if gross_loss else (None if not gross_gain else float('inf')),
            'mdd':mdd, 'mean_pnl':mean([t['pnl'] for t in trades]) if trades else None}


def run_backtest(bars, cfg, name='실험', category='USER'):
    """Price history always chronological; signal based solely on prior close.

    When high AND low hit respective thresholds on the same bar: STOP wins.
    Stop gap through => bar OPEN exit. Target gap through => take at target
    (conservative), fill slippage after threshold; real execution still differs.
    Disallow same-day reentry and use capped positions and finite cash.
    """
    if not bars:
        raise ValueError('백테스트 데이터 없음')
    cash0=float(cfg.get('backtest_initial_cash',1_000_000))
    max_pos=int(cfg.get('max_paper_positions',3))
    qty=int(cfg.get('backtest_qty',1))
    if cash0<=0 or max_pos<=0 or qty<=0:
        raise ValueError('초기 자본·수량·보유종목 제한이 올바르지 않습니다.')
    delay=int(cfg.get('backtest_entry_delay',1))
    if delay not in (1,2,3):
        raise ValueError('진입 지연은 1~3거래일만 지원')
    gap_max=float(cfg.get('backtest_gap_limit',3))
    win=int(cfg.get('backtest_volume_days',20))
    if win<2:
        raise ValueError('평균 거래량 비교기간은 2일 이상이어야 합니다.')
    slip=float(cfg.get('paper_slippage_pct',0.1))/100
    fee=float(cfg.get('paper_fee_pct',0.015))/100
    tax=float(cfg.get('paper_sell_tax_pct',0.2))/100
    stop=float(cfg.get('exit_stop_pct',2))/100
    take=float(cfg.get('exit_take_pct',4))/100
    hold=max(1,int(cfg.get('exit_max_days',3)))
    if min(slip,fee,tax,stop,take)<0 or stop==0 or take==0:
        raise ValueError('손익과 비용 설정값 오류')
    by_code=defaultdict(list)
    by_day=defaultdict(list)
    for b in sorted(bars,key=lambda x:(x.date,x.code)):
        by_code[b.code].append(b)
        by_day[b.date].append(b)
    # Since decisions are after close, schedule on a specific indexed next trading
    # day of that symbol. Never permit same-day signal/entry.
    entries=defaultdict(list)
    candidates=[]
    counters=defaultdict(int)
    for code, lst in by_code.items():
        for i in range(win,len(lst)):
            b=lst[i]
            if cfg.get('market','전체') not in ('전체',b.market):
                continue
            prev=lst[i-1]
            ma=mean(v.close for v in lst[i-win:i])
            av=mean(v.volume for v in lst[i-win:i])
            ratio=100*b.volume/av if av>0 else 0.0
            change=100*(b.close/prev.close-1)
            # Explicitly mark missing volume/cap data. Do not fabricate cap.
            value=b.trade_value_eok
            if value is None:
                value=b.close*b.volume/1e8
                value_origin='종가×거래량 근사'
            else:
                value_origin='실제 CSV 거래대금'
            reasons=[]
            if cfg.get('on_price',False) and not(cfg['min_price']<=b.close<=cfg['max_price']):
                reasons.append('주가')
            if cfg.get('on_cap',False) and (b.cap_eok is None or not(cfg['min_cap_eok']<=b.cap_eok<=cfg['max_cap_eok'])):
                reasons.append('시총 미제공' if b.cap_eok is None else '시총')
            if cfg.get('on_value',False) and value<cfg['min_value_eok']:
                reasons.append('거래대금')
            if ratio<float(cfg.get('backtest_volume_ratio',200)):
                reasons.append('20일 거래량비')
            if cfg.get('on_change',True) and not(cfg['min_change_pct']<=change<=cfg['max_change_pct']):
                reasons.append('등락률')
            if cfg.get('backtest_use_ma',True) and b.close<=ma:
                reasons.append('20일 이평선')
            candidate={'signal_day':b.date,'code':code,'name':b.name,'market':b.market,
                       'signal_close':b.close,'volume_ratio_pct':round(ratio,3),
                       'change_pct':round(change,3),'trade_value_eok':round(value,3),
                       'trade_value_origin':value_origin,'cap_eok':b.cap_eok,
                       'selection':'PASS' if not reasons else 'FAIL',
                       'reason':', '.join(reasons) if reasons else '통과',
                       'entry_status':'NONE','entry_day':'','entry_open':None}
            candidates.append(candidate)
            if reasons:
                counters['screen_fail']+=1
                continue
            counters['signals']+=1
            if i+delay>=len(lst):
                candidate['entry_status']='NO_NEXT_BAR';counters['no_next_bar']+=1
                continue
            enter=lst[i+delay]
            entries[enter.date].append((candidate,b.close,code))
    entry_lookup={(x.date,x.code):i for code,lst in by_code.items() for i,x in enumerate(lst)}
    held={}; cash=cash0; trades=[]; daily=[]
    for day in sorted(by_day):
        quotes={b.code:b for b in by_day[day]}
        # Execute exits for positions already open BEFORE processing new buys.
        def sell(p, b, sellref, reason):
            nonlocal cash
            sellfill=sellref*(1-slip)
            gross=sellfill*p['qty']
            costs=gross*(fee+tax)
            gain=gross-costs-p['entry_cost']
            cash+=gross-costs
            record={**p,'exit_day':day,'exit_ref':sellref,'exit_fill':sellfill,
                    'exit_reason':reason,'pnl':gain,'pnl_pct':gain/p['entry_cost']*100}
            trades.append(record)
            return record
        for code in list(held):
            p=held[code]
            b=quotes.get(code)
            if not b:
                continue
            stop_px=p['entry_fill']*(1-stop)
            take_px=p['entry_fill']*(1+take)
            reason=None;px=None
            if b.open<=stop_px:
                reason='STOP_GAP';px=b.open
            elif b.low<=stop_px:
                reason='STOP';px=stop_px
            elif b.high>=take_px:
                reason='TAKE';px=take_px
            elif entry_lookup[(day,code)]-p['entry_idx']+1>=hold:
                reason='TIME_EXIT';px=b.close
            if reason:
                sell(p,b,px,reason)
                del held[code]
        for candidate, signal_close, code in sorted(entries[day],key=lambda x:x[2]):
            b=quotes[code]
            candidate['entry_day']=day
            candidate['entry_open']=b.open
            gap=100*(b.open/signal_close-1)
            if gap>gap_max:
                candidate['entry_status']='SKIP_GAP';counters['skip_gap']+=1
                continue
            if len(held)>=max_pos:
                candidate['entry_status']='SKIP_CAPACITY';counters['skip_capacity']+=1
                continue
            if code in held:
                candidate['entry_status']='SKIP_ALREADY_HELD';counters['skip_held']+=1
                continue
            buyfill=b.open*(1+slip)
            entry_cost=buyfill*qty*(1+fee)
            if entry_cost>cash:
                candidate['entry_status']='SKIP_CASH';counters['skip_cash']+=1
                continue
            cash-=entry_cost
            candidate['entry_status']='BOUGHT';counters['bought']+=1
            p={'code':code,'name':b.name,'market':b.market,'signal_day':candidate['signal_day'],
               'entry_day':day,'entry_ref':b.open,'entry_fill':buyfill,
               'entry_idx':entry_lookup[(day,code)],'qty':qty,'entry_cost':entry_cost,
               'volume_ratio_pct':candidate['volume_ratio_pct'],'gap_pct':gap,
               'entry_strategy':name}
            stop_px=buyfill*(1-stop)
            take_px=buyfill*(1+take)
            # Entry at the daily open, then extreme prices happen after open.
            if b.low<=stop_px:
                sell(p,b,stop_px,'STOP_ENTRY_DAY')
            elif b.high>=take_px:
                sell(p,b,take_px,'TAKE_ENTRY_DAY')
            elif hold==1:
                sell(p,b,b.close,'TIME_ENTRY_DAY')
            else:
                held[code]=p
        equity=cash
        for code,p in held.items():
            b=quotes.get(code)
            if b:
                px=b.close
            else:
                history=by_code[code]
                # Carry last known close; flagged for incomplete valuations.
                past=[v.close for v in history if v.date<=day]
                px=past[-1] if past else p['entry_ref']
            equity+=px*p['qty']*(1-slip)*(1-fee-tax)
        daily.append({'date':day,'equity':equity,'cash':cash,
                      'open_positions':len(held),'realized_pnl':sum(t['pnl'] for t in trades),
                      'unrealized_pnl':equity-cash-sum(p['entry_cost'] for p in held.values())})
    s=_metric_summary(trades,cash0,daily)
    s.update({'signals':counters['signals'],'bought':counters['bought'],
              'skip_gap':counters['skip_gap'],'skip_capacity':counters['skip_capacity'],
              'skip_cash':counters['skip_cash'],'skip_held':counters['skip_held'],
              'no_next_bar':counters['no_next_bar'],'open_positions':len(held),
              'data_days':len(daily),'first_day':daily[0]['date'],'last_day':daily[-1]['date']})
    return {'name':name,'category':category,'config':dict(cfg),'summary':s,
            'candidates':candidates,'trades':trades,'daily':daily}


def run_experiments(bars,cfg):
    return [run_backtest(bars,v,name,cat) for name,v,cat in variants_from_config(cfg)]


def export_results(directory, runs):
    root=Path(directory);root.mkdir(parents=True,exist_ok=True)
    groups=[('summary.csv', 'summary'),('trades.csv','trades'),('candidates.csv','candidates'),
            ('daily.csv','daily')]
    outputs=[]
    for filename,kind in groups:
        rows=[]
        for run in runs:
            subset=[run['summary']] if kind=='summary' else run[kind]
            for item in subset:
                rows.append({'strategy':run['name'],'category':run['category'],**item})
        columns=list(dict.fromkeys(k for r in rows for k in r))
        path=root/('backtest_'+filename)
        with path.open('w',encoding='utf-8-sig',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=columns)
            writer.writeheader();writer.writerows(rows)
        outputs.append(path)
    return outputs
