"""Frozen displayed recommendations, not delivery, executions or profit claims."""
import json
import math
import re
import hashlib
from dataclasses import asdict
from datetime import datetime
from app.modes import current_mode
from app.trade_review import settings_snapshot
from app.trading_clock import KST
from app.recommendation_outcomes import evaluate
from app.recommendation_overview import SOURCES, CALENDARS, change_label, price_label


def initialize(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS recommendation_batches (
            scan_id INTEGER PRIMARY KEY, created_at TEXT NOT NULL,
            mode TEXT NOT NULL, source TEXT NOT NULL, conditions_json TEXT NOT NULL,
            requested_count INTEGER NOT NULL, selected_count INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS recommendations (
            scan_id INTEGER NOT NULL, rank INTEGER NOT NULL, code TEXT NOT NULL,
            name TEXT NOT NULL, market TEXT NOT NULL, price REAL NOT NULL,
            score REAL NOT NULL, observed_at TEXT NOT NULL, quote_stamp TEXT NOT NULL,
            is_etf INTEGER NOT NULL, PRIMARY KEY(scan_id,code), UNIQUE(scan_id,rank));
        CREATE TABLE IF NOT EXISTS recommendation_reviews (
            id INTEGER PRIMARY KEY, scan_id INTEGER NOT NULL, code TEXT NOT NULL,
            price_source TEXT NOT NULL, calendar_source TEXT NOT NULL,
            available_through TEXT NOT NULL, evidence_hash TEXT NOT NULL,
            created_at TEXT NOT NULL, results_json TEXT NOT NULL,
            UNIQUE(scan_id,code,evidence_hash));
        CREATE INDEX IF NOT EXISTS ix_recommendation_mode ON recommendation_batches(mode,scan_id);
        CREATE INDEX IF NOT EXISTS ix_recommendation_day ON recommendation_batches(mode,date(created_at,'+9 hours'));
        CREATE INDEX IF NOT EXISTS ix_recommendation_latest_review ON recommendation_reviews(scan_id,code,id DESC);
    ''')


def validate_population(stocks,ranked):
    """A saved recommendation must come from the exact saved scan population."""
    try:
        population = {stock.code:stock for stock in stocks}
        codes = [row.stock.code for row in ranked]
        if len(population)!=len(stocks) or len(set(codes))!=len(codes) or set(codes)!=set(population):
            raise ValueError()
        if any(row.stock.as_dict()!=population[row.stock.code].as_dict() for row in ranked):
            raise ValueError()
    except (ValueError,TypeError,AttributeError):
        raise ValueError('검색한 종목 목록과 추천 계산 자료가 맞지 않아요. 검색 기록을 저장하지 않았어요.') from None


def record(db, scan_id, ranked, settings, source, created_at):
    count = settings.get('recommend_count', 15)
    if type(count) is not int or not 1 <= count <= 50:
        raise ValueError('추천 개수는 1~50개여야 합니다.')
    mode = current_mode(settings).key
    if source not in ('DEMO','KIWOOM') or (mode == 'DEMO') != (source == 'DEMO'):
        raise ValueError('추천 모드와 시세 출처가 맞지 않습니다.')
    selected = [r for r in ranked if r.passed][:count]
    rows = []
    seen = set()
    for rank, result in enumerate(selected, 1):
        stock = result.stock
        if (stock.code in seen or not re.fullmatch(r'[A-Za-z0-9]{6}', stock.code)
                or stock.market not in ('KOSPI','KOSDAQ') or stock.source != source
                or type(stock.price) not in (int,float) or not math.isfinite(stock.price) or stock.price <= 0
                or type(result.score) not in (int,float) or not math.isfinite(result.score)):
            raise ValueError('추천 기록의 종목·가격·출처를 확인해야 합니다.')
        seen.add(stock.code)
        rows.append((scan_id,rank,stock.code,stock.name,stock.market,stock.price,result.score,
                     stock.stamp or '',stock.quote_stamp or '',int(stock.is_etf)))
    db.execute('INSERT INTO recommendation_batches VALUES (?,?,?,?,?,?,?)',
               (scan_id,created_at,mode,source,json.dumps(settings_snapshot(settings),ensure_ascii=False,sort_keys=True),
                count,len(rows)))
    db.executemany('INSERT INTO recommendations VALUES (?,?,?,?,?,?,?,?,?,?)', rows)


def recent(db, mode, limit=50, *, offset=0):
    if (mode not in ('DEMO','MOCK','REAL_QUOTES') or type(limit) is not int or not 1 <= limit <= 200
            or type(offset) is not int or not 0<=offset<=5_000_000):
        raise ValueError('추천 기록 조회 범위를 확인해 주세요.')
    return [dict(row) for row in db.execute('''SELECT b.created_at,b.mode,b.source,b.conditions_json,
        r.* FROM recommendations r JOIN recommendation_batches b ON b.scan_id=r.scan_id
        WHERE b.mode=? ORDER BY b.scan_id DESC,r.rank LIMIT ? OFFSET ?''', (mode,limit,offset))]


def review(db, *, scan_id, code, mode, bars, sessions, available_through, price_source):
    row = db.execute('''SELECT b.mode,b.created_at,r.price,r.market FROM recommendations r
        JOIN recommendation_batches b ON b.scan_id=r.scan_id WHERE r.scan_id=? AND r.code=?''',
        (scan_id,code)).fetchone()
    if row is None or row['mode'] != mode:
        raise ValueError('같은 모드의 저장된 추천을 먼저 확인해 주세요.')
    results = evaluate(mode=mode,code=code,market=row['market'],entry_price=row['price'],
        recommended_at=datetime.fromisoformat(row['created_at']),bars=bars,sessions=sessions,
        available_through=available_through,price_source=price_source)
    payload = json.dumps([asdict(r) for r in results],sort_keys=True,allow_nan=False)
    proof = json.dumps(dict(results=payload,sessions=asdict(sessions),through=available_through,
                            source=price_source),sort_keys=True)
    fingerprint = hashlib.sha256(proof.encode()).hexdigest()
    db.execute('''INSERT OR IGNORE INTO recommendation_reviews(scan_id,code,price_source,calendar_source,
        available_through,evidence_hash,created_at,results_json) VALUES(?,?,?,?,?,?,?,?)''',
        (scan_id,code,price_source,sessions.source,available_through,fingerprint,
         datetime.now(KST).isoformat(timespec='seconds'),payload))
    return results


def page_count(db,mode):
    if mode not in ('DEMO','MOCK','REAL_QUOTES'):
        raise ValueError('추천 모드를 확인해 주세요.')
    count = db.execute('''SELECT COUNT(*) FROM recommendations r
        JOIN recommendation_batches b ON b.scan_id=r.scan_id WHERE b.mode=?''',(mode,)).fetchone()[0]
    return max(1,(count+4)//5)


def summary(db, mode, *, performance=False,page=1):
    if type(page) is not int or not 1<=page<=999999:
        raise ValueError('추천 기록 페이지 번호를 확인해 주세요.')
    pages = page_count(db,mode)
    command = '/추천성적' if performance else '/추천기록'
    if page>pages:
        return f'추천 기록은 {pages}페이지까지 있어요. {command} {pages}'
    rows = recent(db,mode,5,offset=(page-1)*5)
    label = {'DEMO':'가상 연습','MOCK':'키움 모의 시세','REAL_QUOTES':'실제 시세'}[mode]
    lines = [f'[{label} · '+('추천 이후 가격 관측' if performance else '추천 기록')+f' · {page}/{pages}]',
             '최근 순 5개씩 · 목록 생성 기록이며 텔레그램 발송·체결 증명이 아니에요.']
    if not rows:
        return '\n'.join(lines+['저장된 추천이 없어요. 이 기능 추가 뒤 완료한 검색부터 기록해요.'])
    labels = {'OBSERVED':'관측됨','NOT_DUE':'아직 날짜 전','PRICE_MISSING':'가격 자료 없음',
              'CALENDAR_MISSING':'거래일 자료 부족'}
    for row in rows:
        name = ''.join(c for c in row['name'] if c.isprintable())[:24]
        lines.append(f"검색 #{row['scan_id']} {row['created_at'][:16]} · {row['rank']}위 {name}({row['code']}) · {price_label(row['price'])}")
        if not performance:
            continue
        seen = db.execute('''SELECT * FROM recommendation_reviews WHERE scan_id=? AND code=?
            ORDER BY id DESC LIMIT 1''', (row['scan_id'],row['code'])).fetchone()
        if not seen:
            lines.append('1·3·5거래일: 확인된 평가 자료 없음')
            continue
        lines.append(f"{SOURCES[seen['price_source']]} · {CALENDARS[seen['calendar_source']]} · 확인 완료일 {seen['available_through']}")
        for result in json.loads(seen['results_json']):
            detail = (change_label(result['change_pct']) if result['state']=='OBSERVED'
                      else labels[result['state']])
            lines.append(f"{result['horizon']}거래일 ({result['target_day'] or '미확인'}): {detail}")
    if performance:
        lines.append('추천 당시 마지막 시세 대비 제공된 종가 변화예요. 비용·배당·분할 미반영으로 실제 수익률·예측이 아니에요.')
    else:
        lines.append('/추천성적: 확인된 1·3·5거래일 가격 관측')
    if page<pages and page<999999:
        lines.append(f'다음 기록: {command} {page+1}')
    if page>1:
        lines.append(f'이전 기록: {command} {page-1}')
    return '\n'.join(lines)
