"""Offline daily-cache detection and durable alert preparation. Never sends orders/messages."""
from collections import defaultdict
from hashlib import sha256
import json
from datetime import date
from app.theme_one import detect, alert_text
from app.recommendation_outcomes import session_day
from app.nxt_evidence import latest_for_day


def prepare(history, *, sessions, through, mode, source):
    """Use one bounded cache read; retain evidence corrections as new revisions."""
    if (mode not in ('DEMO','MOCK','REAL_QUOTES') or source not in ('DEMO','KRX','CSV_USER')
            or (mode=='DEMO')!=(source=='DEMO')):
        raise ValueError('자료 모드와 출처를 확인해 주세요.')
    _,_,days=sessions.validate(mode)
    if session_day(through) not in days:
        raise ValueError('감지일은 확인된 거래일이어야 해요.')
    eligible=[d for d in days if d.isoformat()<=through]
    if len(eligible)<7:
        raise ValueError('전일 종가를 포함한 7거래일 근거가 필요해요.')
    bars,observations,nxt_rows=history.load_evidence(source,eligible[-7].isoformat(),through,nxt_mode=mode)
    nxt=latest_for_day(nxt_rows,mode,through)
    expected_days={d.isoformat() for d in eligible[-7:]}
    observed_market_days={(b.market,b.date) for b in bars}
    missing_market_days=[(market,day) for market in ('KOSPI','KOSDAQ') for day in sorted(expected_days)
                         if (market,day) not in observed_market_days]
    observed={(row['market'],row['day']):row for row in observations}
    price_counts=defaultdict(int)
    for bar in bars:
        price_counts[(bar.market,bar.date)]+=1
    quality={}
    for market in ('KOSPI','KOSDAQ'):
        unknown=omitted=stale=0
        for day in expected_days:
            row=observed.get((market,day))
            if row is None or row['received_count'] is None:
                unknown+=1
            else:
                counts=(row['received_count'],row['kept_count'],row['omitted_count'])
                if (not all(type(value) is int and value>=0 for value in counts)
                        or counts[0]!=counts[1]+counts[2]):
                    raise ValueError('일별 정규화 관측 수를 확인해야 해요.')
                omitted+=counts[2]
                if counts[1]!=price_counts[(market,day)]:
                    stale+=1
        quality[market]=dict(unknown_days=unknown,omitted_rows=omitted,stale_days=stale)
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
            result['ingest_quality']=quality[market]
            evidence=nxt.get((market,code))
            result['nxt_evidence']=(dict(eligible=True if evidence['eligible']==1 else None,
                                         day=evidence['day'],observed_at=evidence['observed_at'],
                                         source='키움 종목정보 목록(ka10099) 관측') if evidence else None)
            results.append(result)
    results.sort(key=lambda r:(-r['hits'][-1]['trade_value_eok'],r['market'],r['code']))
    with history.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('''CREATE TABLE IF NOT EXISTS theme_alerts (
            id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE, mode TEXT NOT NULL,
            source TEXT NOT NULL, day TEXT NOT NULL, market TEXT NOT NULL, code TEXT NOT NULL,
            result_json TEXT NOT NULL, message TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'PREPARED')''')
        db.execute('''CREATE TABLE IF NOT EXISTS theme_runs (
            id INTEGER PRIMARY KEY, mode TEXT NOT NULL, source TEXT NOT NULL, day TEXT NOT NULL,
            report_json TEXT NOT NULL, alert_ids_json TEXT NOT NULL)''')
        inserted=0
        alert_ids=[]
        for result in results:
            evidence=result['nxt_evidence']
            message=alert_text(result,nxt_eligible=evidence['eligible'] if evidence else None,
                               nxt_asof=evidence['day'] if evidence else None,nxt_source=evidence['source'] if evidence else None)
            payload=json.dumps(dict(theme=1,version=1,mode=mode,result=result),sort_keys=True,ensure_ascii=False,allow_nan=False)
            key=sha256(payload.encode()).hexdigest()
            cur=db.execute('''INSERT OR IGNORE INTO theme_alerts
                (fingerprint,mode,source,day,market,code,result_json,message) VALUES(?,?,?,?,?,?,?,?)''',
                (key,mode,source,through,result['market'],result['code'],payload,message))
            inserted+=cur.rowcount
            alert_ids.append(db.execute('SELECT id FROM theme_alerts WHERE fingerprint=?',(key,)).fetchone()[0])
        report=dict(symbols=len(grouped),states=dict(states),matched=len(results),prepared=inserted,
                    missing_market_days=missing_market_days,coverage='CACHED_SYMBOLS_ONLY',ingest_quality=quality)
        db.execute('INSERT INTO theme_runs(mode,source,day,report_json,alert_ids_json) VALUES(?,?,?,?,?)',
                   (mode,source,through,json.dumps(report,sort_keys=True),json.dumps(alert_ids)))
    return dict(report,results=results)


def latest_report(history, *, mode, source=None, page=1):
    """Read only latest run membership, never resurrect old matches after corrections."""
    if type(page) is not int or not 1<=page<=999999:
        return '페이지 번호를 확인해 주세요.'
    with history.connect() as db:
        db.execute('BEGIN')
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='theme_runs'").fetchone():
            return '테마1 판정 기록이 없어요. 자료를 확인한 뒤 먼저 판정해 주세요.'
        run=db.execute('SELECT * FROM theme_runs WHERE mode=?'+(' AND source=?' if source else '')+' ORDER BY id DESC LIMIT 1',
                       (mode,source) if source else (mode,)).fetchone()
        if run is None:
            return '이 모드·자료 출처의 테마1 판정 기록이 없어요.'
        source=run['source']
        report=json.loads(run['report_json'])
        ids=json.loads(run['alert_ids_json'])
        header=(f'[테마1 · 저장된 판정] 기준일 {run["day"]} · 자료 {source}\n'
                f'저장된 종목 {report["symbols"]}개 · 감지 {report["matched"]}개\n'
                f'자료 부족 종목 {report["states"].get("PRICE_MISSING",0)}개 · '
                f'시장별 일별 자료가 없는 구간 {len(report["missing_market_days"])}개\n'
                '전체 시장 수집 완료는 미확인입니다. 발송·주문 완료 기록이 아닙니다.\n')
        if not ids:
            return header+'이 판정에서 감지된 종목이 없어요. 자료 부족 여부를 함께 확인하세요.'
        if page>len(ids):
            return header+f'마지막 페이지는 {len(ids)}예요.'
        row=db.execute('SELECT message FROM theme_alerts WHERE id=? AND mode=? AND source=?',
                       (ids[page-1],mode,source)).fetchone()
        return header+f'{page}/{len(ids)}페이지\n'+row['message']+(f'\n다음: /테마1 {page+1}' if page<len(ids) else '')
