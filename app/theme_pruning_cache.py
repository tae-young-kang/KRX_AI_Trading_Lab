"""Historical lookup hints only; never current Theme1 detection evidence."""
from datetime import datetime,time
from app.backtest import Bar
from app.krx_history import validate_bars
from app.trading_clock import KST
from app.theme_sources import KIWOOM_SOURCES


def load(history,source,days,now):
    """Reuse same-server history captured after the data day or after 20:00 KST.

    Current detection still fetches every candidate's full seven bars. Retaining
    eliminated symbols here avoids alternating full scans as detection replaces
    its own window. This table must not be consumed as detection/alert evidence.
    """
    records={}
    with history.connect() as db:
        tables=['daily_bars']
        for table in ('theme_pruning_bars','theme_pruning_checkpoints'):
            if db.execute("SELECT 1 FROM sqlite_master WHERE name=? AND type='table'",(table,)).fetchone():
                tables.append(table)
        rows=[]
        for table in tables:
            rows.extend(dict(r) for r in db.execute(
                f'SELECT * FROM {table} WHERE source=? AND day BETWEEN ? AND ?',
                (source,days[0],days[-1])))
    for row in sorted(rows,key=lambda r:r['fetched_at']):
        key=(row['market'],row['code'],row['day'])
        try:
            stamp=datetime.fromisoformat(row['fetched_at'])
            # Old timezone-less cache cannot prove a closed-day snapshot.
            if stamp.tzinfo is None or stamp.utcoffset() is None or stamp>now:continue
            local=stamp.astimezone(KST)
            if row['day']>local.date().isoformat():continue
            if row['day']==local.date().isoformat() and local.time()<time(20):continue
            bar=Bar(*(row[k] for k in ('day','code','name','market','open','high','low','close','volume','trade_value_eok','cap_eok')))
            validate_bars([bar])
            records[key]=bar
        except (TypeError,ValueError,KeyError):
            records.pop(key,None)
    return list(records.values())


def checkpoint(history,source,bars,stamp):
    """Persist completed past-symbol hints independently of detection commits.

    The collector passes only bars before its through day. This writer also
    rejects today's data. Interrupted/failed searches never publish a new
    detection window, run, alert or NXT observation through this table.
    """
    if source not in KIWOOM_SOURCES.values():raise ValueError('조회 참고 자료의 서버를 확인해 주세요.')
    moment=datetime.fromisoformat(stamp)
    if moment.tzinfo is None or moment.utcoffset() is None:raise ValueError('조회 참고 자료의 시간대를 확인해 주세요.')
    records=validate_bars(bars)
    today=moment.astimezone(KST).date().isoformat()
    if any(bar.date>=today for bar in records):raise ValueError('과거 일봉만 조회 참고 자료로 저장할 수 있어요.')
    if not records:return
    with history.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE TABLE IF NOT EXISTS theme_pruning_checkpoints AS SELECT * FROM daily_bars WHERE 0')
        db.execute('CREATE UNIQUE INDEX IF NOT EXISTS ix_theme_pruning_checkpoint ON theme_pruning_checkpoints(source,day,market,code)')
        db.executemany('''INSERT OR REPLACE INTO theme_pruning_checkpoints
            (source,day,market,code,name,open,high,low,close,volume,trade_value_eok,cap_eok,fetched_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',[
            (source,b.date,b.market,b.code,b.name,b.open,b.high,b.low,b.close,b.volume,b.trade_value_eok,None,stamp)
            for b in records])


def retain(db,source,bars,stamp):
    """Called inside the collector's existing transaction, after validation."""
    db.execute('CREATE TABLE IF NOT EXISTS theme_pruning_bars AS SELECT * FROM daily_bars WHERE 0')
    db.execute('CREATE UNIQUE INDEX IF NOT EXISTS ix_theme_pruning ON theme_pruning_bars(source,day,market,code)')
    # Seed the previous version too, before detection replaces its window.
    db.execute('INSERT OR IGNORE INTO theme_pruning_bars SELECT * FROM daily_bars WHERE source=?',(source,))
    db.executemany('''INSERT OR REPLACE INTO theme_pruning_bars
        (source,day,market,code,name,open,high,low,close,volume,trade_value_eok,cap_eok,fetched_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',[
        (source,b.date,b.market,b.code,b.name,b.open,b.high,b.low,b.close,b.volume,b.trade_value_eok,b.cap_eok,stamp)
        for b in bars])
    # Freshly revalidated symbols now have a durable completed-search copy.
    # Keep interrupted hints for symbols not queried on this completed search.
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='theme_pruning_checkpoints' AND type='table'").fetchone():
        db.executemany('DELETE FROM theme_pruning_checkpoints WHERE source=? AND day=? AND market=? AND code=?',
                       [(source,b.date,b.market,b.code) for b in bars])
