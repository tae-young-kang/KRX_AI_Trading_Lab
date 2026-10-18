"""Frozen displayed recommendations, not delivery, executions or profit claims."""
import json
import math
import re
from app.modes import current_mode
from app.trade_review import settings_snapshot


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
    ''')


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


def recent(db, mode, limit=50):
    if mode not in ('DEMO','MOCK','REAL_QUOTES') or type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError('추천 기록 조회 범위를 확인해 주세요.')
    return [dict(row) for row in db.execute('''SELECT b.created_at,b.mode,b.source,b.conditions_json,
        r.* FROM recommendations r JOIN recommendation_batches b ON b.scan_id=r.scan_id
        WHERE b.mode=? ORDER BY b.scan_id DESC,r.rank LIMIT ?''', (mode,limit))]
