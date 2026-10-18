"""Explicit local price/session package; never official certification or IO to servers."""
from dataclasses import dataclass
from datetime import date, datetime
import json
from math import isfinite
import re
from app.recommendation_outcomes import SessionEvidence
from app.recommendations import review
from app.trading_clock import KST


@dataclass(frozen=True)
class Price:
    date: str
    code: str
    market: str
    close: float


def load_package(path, mode, *, today=None):
    with open(path,'rb') as stream:
        raw = stream.read(2_000_001)
    if len(raw)>2_000_000:
        raise ValueError('자료 파일은 2MB 이하여야 해요.')
    try:
        data = json.loads(raw.decode('utf-8-sig'))
        required = {'version','mode','price_source','calendar','available_through','prices'}
        if not isinstance(data,dict) or set(data)!=required or type(data['version']) is not int or data['version']!=1:
            raise ValueError()
        if mode not in ('DEMO','MOCK','REAL_QUOTES') or data['mode']!=mode:
            raise ValueError()
        expected = ('DEMO','DEMO') if mode=='DEMO' else ('CSV_USER','USER_CONFIRMED')
        calendar = data['calendar']
        if not isinstance(calendar,dict) or set(calendar)!={'days','begin','end','source'}:
            raise ValueError()
        if not isinstance(calendar['days'],list) or not all(isinstance(day,str) for day in calendar['days']):
            raise ValueError()
        if (data['price_source'],calendar['source'])!=expected:
            raise ValueError()
        sessions = SessionEvidence(tuple(calendar['days']),calendar['begin'],calendar['end'],calendar['source'])
        begin,end,days = sessions.validate(mode)
        through = date.fromisoformat(data['available_through'])
        if (not begin<=through<=end or through>(today or datetime.now(KST).date())
                or not isinstance(data['prices'],list) or len(data['prices'])>50000):
            raise ValueError()
        prices,seen = [],set()
        day_set = set(days)
        for row in data['prices']:
            if not isinstance(row,dict) or set(row)!={'day','code','market','close'}:
                raise ValueError()
            day = date.fromisoformat(row['day'])
            code,market,close = row['code'],row['market'],row['close']
            key = (row['day'],code,market)
            if (not isinstance(code,str) or not re.fullmatch(r'[A-Za-z0-9]{6}',code)
                    or market not in ('KOSPI','KOSDAQ') or key in seen or day not in day_set
                    or type(close) not in (int,float) or not isfinite(close) or close<=0):
                raise ValueError()
            seen.add(key)
            prices.append(Price(row['day'],code,market,float(close)))
        return data,sessions,prices
    except (ValueError,TypeError,KeyError,OverflowError,UnicodeError):
        raise ValueError('자료 형식·모드·거래일·가격을 확인해 주세요. 공식 자료 표시는 파일로 등록할 수 없어요.') from None


def import_package(database, path, mode, *, today=None):
    data,sessions,prices = load_package(path,mode,today=today)
    through = date.fromisoformat(data['available_through'])
    begin = date.fromisoformat(sessions.begin)
    indexed = {}
    for price in prices:
        indexed.setdefault((price.code,price.market),[]).append(price)
    eligible = []
    with database.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        rows = db.execute('''SELECT b.scan_id,b.created_at,r.code,r.market FROM recommendation_batches b
            JOIN recommendations r ON r.scan_id=b.scan_id WHERE b.mode=?''',(mode,)).fetchall()
        for row in rows:
            moment = datetime.fromisoformat(row['created_at'])
            if moment.tzinfo is None:
                continue
            if begin<=moment.astimezone(KST).date()<=through:
                eligible.append(row)
        if not eligible:
            raise ValueError('확인한 기간에 같은 모드의 추천이 없어요. 기존 자료는 바꾸지 않았어요.')
        before = db.execute('SELECT COUNT(*) FROM recommendation_reviews').fetchone()[0]
        for row in eligible:
            review(db,scan_id=row['scan_id'],code=row['code'],mode=mode,
                   bars=indexed.get((row['code'],row['market']),[]),sessions=sessions,
                   available_through=data['available_through'],price_source=data['price_source'])
        after = db.execute('SELECT COUNT(*) FROM recommendation_reviews').fetchone()[0]
    return dict(recommendations=len(eligible),added=after-before,available_through=data['available_through'])
