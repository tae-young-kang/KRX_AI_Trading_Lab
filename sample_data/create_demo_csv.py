"""Deterministic SYNTHETIC history for GUI tests. NOT investment market data."""
import csv
import random
from datetime import date,timedelta
from pathlib import Path


def build(destination=None):
    destination=Path(destination or Path(__file__).with_name('DEMO_synthetic_ohlcv.csv'))
    rng=random.Random(2026)
    portfolio=[('900101','가상 코스피 반도체','KOSPI',65000,50000,500000),
               ('900102','가상 코스피 기계','KOSPI',25000,9000,650000),
               ('900201','가상 코스닥 바이오','KOSDAQ',32000,5000,400000),
               ('900202','가상 코스닥 IT','KOSDAQ',18000,3000,750000)]
    days=[]; current=date(2026,1,5)
    while len(days)<110:
        if current.weekday()<5:days.append(current)
        current+=timedelta(days=1)
    with destination.open('w',newline='',encoding='utf-8-sig') as out:
        w=csv.writer(out)
        w.writerow(['date','code','name','market','open','high','low','close','volume','trade_value_eok','cap_eok'])
        for code,name,mrkt,price,cap,basevol in portfolio:
            for i,day in enumerate(days):
                prev=price
                # Artificial breakout roughly twice monthly, not derived from market.
                impulse=0.042 if i in (22,35,47,65,78,94,103) else rng.uniform(-.022,.020)
                close=round(prev*(1+impulse),0)
                opening=round(prev*(1+rng.uniform(-.018,.022)),0)
                low=round(min(close,opening)*(1-rng.uniform(.004,.03)),0)
                high=round(max(close,opening)*(1+rng.uniform(.004,.033)),0)
                volume=int(basevol*(3.2 if i in (22,35,47,65,78,94,103) else rng.uniform(.7,1.3)))
                value=round((opening+close)/2*volume/1e8,2)
                w.writerow([day.isoformat(),code,name,mrkt,int(opening),int(high),int(low),
                            int(close),volume,value,cap])
                price=close
    return destination


if __name__=='__main__':
    print(build())
