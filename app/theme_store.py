"""Offline daily-cache detection and durable alert preparation. Never sends orders/messages."""
from collections import defaultdict
from hashlib import sha256
import json
from datetime import date
from app.theme_one import detect, alert_text


def prepare(history, *, sessions, through, mode, source):
    """Use one bounded cache read; retain evidence corrections as new revisions."""
    if (mode not in ('DEMO','MOCK','REAL_QUOTES') or source not in ('DEMO','KRX','CSV_USER')
            or (mode=='DEMO')!=(source=='DEMO')):
        raise ValueError('자료 모드와 출처를 확인해 주세요.')
    _,_,days=sessions.validate(mode)
    if date.fromisoformat(through) not in days:
        raise ValueError('감지일은 확인된 거래일이어야 해요.')
    eligible=[d for d in days if d.isoformat()<=through]
    if len(eligible)<7:
        raise ValueError('전일 종가를 포함한 7거래일 근거가 필요해요.')
    bars=history.load_bars(source,eligible[-7].isoformat(),through)
    grouped=defaultdict(list)
    for bar in bars:
        if bar.market in ('KOSPI','KOSDAQ'):
            grouped[(bar.market,bar.code)].append(bar)
    results=[]
    states=defaultdict(int)
    for (market,code),rows in sorted(grouped.items()):
        result=detect(code=code,market=market,bars=rows,sessions=sessions,through=through,
                      mode=mode,price_source=source)
        states[result['status']]+=1
        if result['matched']:
            results.append(result)
    results.sort(key=lambda r:(-r['hits'][-1]['trade_value_eok'],r['market'],r['code']))
    with history.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('''CREATE TABLE IF NOT EXISTS theme_alerts (
            id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE, mode TEXT NOT NULL,
            source TEXT NOT NULL, day TEXT NOT NULL, market TEXT NOT NULL, code TEXT NOT NULL,
            result_json TEXT NOT NULL, message TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'PREPARED')''')
        inserted=0
        for result in results:
            payload=json.dumps(dict(theme=1,version=1,mode=mode,result=result),sort_keys=True,ensure_ascii=False,allow_nan=False)
            key=sha256(payload.encode()).hexdigest()
            cur=db.execute('''INSERT OR IGNORE INTO theme_alerts
                (fingerprint,mode,source,day,market,code,result_json,message) VALUES(?,?,?,?,?,?,?,?)''',
                (key,mode,source,through,result['market'],result['code'],payload,alert_text(result)))
            inserted+=cur.rowcount
    return dict(symbols=len(grouped),states=dict(states),matched=len(results),prepared=inserted,
                coverage='CACHED_SYMBOLS_ONLY',results=results)
