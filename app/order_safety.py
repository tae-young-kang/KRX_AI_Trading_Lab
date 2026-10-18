"""Durable limits shared by every Telegram identity on the same fake account."""
import uuid
from dataclasses import dataclass
from app.order_engine import OrderError
from app.trading_clock import KST


@dataclass(frozen=True)
class PracticeLimits:
    daily_count: int = 20
    daily_notional: int = 2_000_000

    def __post_init__(self):
        if any(type(v) is not int or v <= 0 for v in (self.daily_count, self.daily_notional)):
            raise ValueError('연습 한도는 양의 정수여야 합니다.')


def initialize(db, broker):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS tg_risk(broker TEXT PRIMARY KEY,halted INTEGER NOT NULL,
            halt_id TEXT,reason TEXT,revision INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS tg_usage(intent_id TEXT PRIMARY KEY,broker TEXT,day TEXT,notional INTEGER);
        CREATE INDEX IF NOT EXISTS tg_usage_day ON tg_usage(broker,day);
        CREATE TABLE IF NOT EXISTS tg_guard(key TEXT PRIMARY KEY,value TEXT);
    ''')
    db.execute("INSERT OR IGNORE INTO tg_guard VALUES('broker',?)", (broker,))
    if db.execute("SELECT value FROM tg_guard WHERE key='broker'").fetchone()[0] != broker:
        raise OrderError('다른 연습 계좌의 안전 기록을 사용할 수 없습니다.')
    if 'revision' not in {r['name'] for r in db.execute('PRAGMA table_info(tg_risk)')}:
        db.execute('ALTER TABLE tg_risk ADD COLUMN revision INTEGER NOT NULL DEFAULT 0')
    db.execute('INSERT OR IGNORE INTO tg_risk(broker,halted,halt_id,reason) VALUES(?,0,NULL,NULL)', (broker,))


def usage(db, broker, moment):
    day = moment.astimezone(KST).date().isoformat()
    row = db.execute('SELECT COUNT(*),COALESCE(SUM(notional),0) FROM tg_usage WHERE broker=? AND day=?',
                     (broker, day)).fetchone()
    return dict(day=day, count=row[0], notional=row[1])


def reserve(db, broker, intent_id, request, moment, limits):
    if db.execute('SELECT halted FROM tg_risk WHERE broker=?', (broker,)).fetchone()[0]:
        raise OrderError('긴급 중지 상태입니다. 프로그램에서 복구 확인 후 재개해야 합니다.')
    if db.execute('SELECT 1 FROM tg_usage WHERE intent_id=?', (intent_id,)).fetchone():
        raise OrderError('이미 확인 처리한 요청입니다.')
    used = usage(db, broker, moment)
    amount = request.quantity * request.limit_price
    if used['count'] >= limits.daily_count or used['notional']+amount > limits.daily_notional:
        raise OrderError('오늘의 확인 횟수·누적 주문금액 한도를 넘습니다. 취소·재시작으로 한도가 복원되지 않습니다.')
    db.execute('INSERT INTO tg_usage VALUES(?,?,?,?)', (intent_id, broker, used['day'], amount))


def latch(db, broker, reason):
    row = db.execute('SELECT * FROM tg_risk WHERE broker=?', (broker,)).fetchone()
    generation = row['halt_id'] if row['halted'] else uuid.uuid4().hex
    db.execute('UPDATE tg_risk SET halted=1,halt_id=?,reason=?,revision=revision+1 WHERE broker=?', (generation, reason, broker))
    db.execute("UPDATE tg_intents SET state='CANCELED' WHERE state='WAITING'")
    return generation
