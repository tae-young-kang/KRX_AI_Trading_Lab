"""Fully SYNTHETIC example, NEVER actual Korea Exchange prices."""
from __future__ import annotations
import csv
import math
import random
from datetime import date,timedelta
from pathlib import Path

def generate(path=None):
    rng=random.Random(260016)
    day=date(2026,3,2);days=[]
    while len(days)<116:
        if day.weekday()<5:days.append(day.isoformat())
        day+=timedelta(days=1)
    dest=Path(path) if path else Path(__file__).parent/'DEMO_research_ohlcv.csv'
    with dest.open('w',encoding='utf-8-sig',newline='') as f:
        out=csv.writer(f)
        out.writerow(['date','code','name','market','open','high','low','close','volume','trade_value_eok','cap_eok'])
        for stock in range(96):
            market='KOSPI' if stock<48 else 'KOSDAQ'
            code=f'{900000+stock:06d}'
            name='DEMO_가상주식_'+str(stock+1)
            price=rng.uniform(3000,98000)
            outstanding=rng.uniform(5e6,1.3e8)
            basevol=rng.uniform(50000,900000)
            bias=(stock % 12 - 6) / 15000
            for dayidx,d in enumerate(days):
                burst = rng.uniform(1.5,4.1) if rng.random()<.15 else rng.uniform(.65,1.4)
                vol=max(1,round(basevol*burst*rng.uniform(.85,1.15)))
                ret=rng.gauss(bias,.021)+(0.007 if burst>2.2 else 0)
                o=max(100,price*rng.uniform(.98,1.02))
                c=max(100,o*(1+ret))
                h=max(o,c)*(1+rng.uniform(.001,.021))
                l=min(o,c)*(1-rng.uniform(.001,.021))
                price=c
                out.writerow([d,code,name,market,round(o,2),round(h,2),round(l,2),
                              round(c,2),vol,round(vol*(o+c)/2/1e8,3),round(c*outstanding/1e8,2)])
    return str(dest)
if __name__=='__main__':print('SYNTHETIC ONLY:',generate())
