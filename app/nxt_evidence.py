"""Read-only eligibility observations; never order permission or historical inference."""
from datetime import datetime
import re
from app.trading_clock import KST


def latest_for_day(rows,mode,day):
    """Validate persisted context before using a positive observation in an alert."""
    latest={}
    try:
        for row in rows:
            stamp=datetime.fromisoformat(row['observed_at'])
            if (row['mode']!=mode or row['day']!=day or stamp.tzinfo is None
                    or stamp.astimezone(KST).date().isoformat()!=day
                    or row['market'] not in ('KOSPI','KOSDAQ')
                    or not re.fullmatch(r'[A-Za-z0-9]{6}',row['code'])
                    or row['eligible'] is not None and (type(row['eligible']) is not int or row['eligible']!=1)):
                raise ValueError()
            key=(row['market'],row['code'])
            old=latest.get(key)
            if old is None or stamp>datetime.fromisoformat(old['observed_at']):
                latest[key]=row
            elif stamp==datetime.fromisoformat(old['observed_at']) and row['eligible']!=old['eligible']:
                raise ValueError()
    except (TypeError,ValueError,KeyError,OverflowError):
        raise ValueError('저장된 NXT 관측의 날짜·시각·종목·가능 여부를 확인해야 해요.') from None
    return latest


def record(history,mode,stocks,*,now=None):
    if mode not in ('DEMO','MOCK','REAL_QUOTES'):
        raise ValueError('NXT 관측 모드를 확인해 주세요.')
    current=now or datetime.now(KST)
    if current.tzinfo is None:
        raise ValueError('NXT 관측 기준 시각을 확인해 주세요.')
    rows=[]
    for stock in stocks:
        if getattr(stock,'nxt_source','')!='KIWOOM_KA10099':
            continue
        stamp=datetime.fromisoformat(stock.nxt_observed_at)
        if (mode=='DEMO' or stock.source!='KIWOOM' or stamp.tzinfo is None or stamp>current
                or stock.nxt_eligible is not None and stock.nxt_eligible is not True
                or not re.fullmatch(r'[A-Za-z0-9]{6}',stock.code) or stock.market not in ('KOSPI','KOSDAQ')):
            raise ValueError('NXT 관측의 모드·시각·종목을 확인해 주세요.')
        stamp=stamp.astimezone(KST)
        rows.append((mode,stock.market,stock.code,stamp.date().isoformat(),stamp.isoformat(),
                     1 if stock.nxt_eligible is True else None))
    if not rows:
        return 0
    with history.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        for row in rows:
            prior=db.execute('SELECT eligible FROM nxt_observations WHERE mode=? AND market=? AND code=? AND observed_at=?',
                             (row[0],row[1],row[2],row[4])).fetchone()
            if prior and prior['eligible']!=row[5]:
                raise ValueError('같은 시각의 NXT 관측 기록이 달라요. 자동으로 덮지 않았어요.')
            db.execute('''INSERT OR IGNORE INTO nxt_observations
                (mode,market,code,day,observed_at,eligible) VALUES(?,?,?,?,?,?)''',row)
    return len(rows)
