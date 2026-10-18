"""Durable fail-closed reasons only; no secrets, orders, or unlock operation."""
import json
from app.trading_clock import KST

REASONS={
    'LEDGER_CONFLICT':'이전에 확인한 체결이 새 조회에서 누락되거나 바뀌었어요.',
    'ORDER_CONFLICT':'접수한 주문과 조회된 종목·방향·수량이 맞지 않아요.',
    'CHAIN_CONFLICT':'정정·취소의 원주문 연결 또는 체결 기록을 대조하지 못했어요.',
    'CONTEXT_CONFLICT':'조회 계좌·인증·날짜가 현재 모의 연결과 달라요.',
}


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS safety_latch (id INTEGER PRIMARY KEY CHECK(id=1),reason TEXT,checked_at TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS observation_blocks (id TEXT PRIMARY KEY)')


def load(db):
    row=db.execute('SELECT reason,checked_at FROM safety_latch WHERE id=1').fetchone()
    return dict(row) if row else None


def latch(db,reason,now):
    if reason not in REASONS: raise ValueError('알 수 없는 모의 점검 사유입니다.')
    db.execute('INSERT OR IGNORE INTO safety_latch VALUES (1,?,?)',
               (reason,now.astimezone(KST).strftime('%Y/%m/%d %H:%M:%S')))
    db.execute("UPDATE intents SET state='CANCELED' WHERE state IN ('WAITING','QUEUED')")


def summary(db,*,armed,open_now,day,now):
    saved=load(db)
    halted=db.execute('SELECT stopped_at FROM manual_halt WHERE id=1').fetchone()
    unknown=db.execute("SELECT COUNT(*) FROM intents WHERE state='UNKNOWN'").fetchone()[0]
    processing=db.execute("SELECT COUNT(*) FROM intents WHERE state='CLAIMED'").fetchone()[0]
    pending=db.execute("SELECT COUNT(*) FROM intents WHERE state IN ('WAITING','QUEUED')").fetchone()[0]
    waiting=db.execute('SELECT COUNT(*) FROM observation_blocks b JOIN intents i ON i.id=b.id WHERE i.day=?',
                       (day.isoformat(),)).fetchone()[0]
    cancellations=sum(json.loads(row[0]).get('action')=='CANCEL' for row in db.execute(
        "SELECT payload FROM intents WHERE state='ACKNOWLEDGED' AND day=?",(day.isoformat(),)))
    lines=['[키움 모의 주문 점검 · 실제 돈 주문 잠금]',
           '점검 시각: '+now.astimezone(KST).strftime('%Y/%m/%d %H:%M:%S')]
    if saved:
        lines.extend(['새 모의 주문: 기록 충돌로 차단',
                      REASONS.get(saved['reason'],'저장된 모의 기록을 확인해야 해요.'),
                      '차단 기록 시각: '+saved['checked_at'],
                      '프로그램을 다시 켜도 차단을 유지해요. 자동 해제하지 않아요.'])
    elif unknown:
        lines.extend(['새 모의 주문: 미확인 전송으로 차단',
                      '증권사에서 처리됐을 수 있어요. 같은 주문을 다시 보내지 마세요.'])
    elif processing:
        lines.append('모의 주문 처리 중이에요. 새 요청 없이 /주문상태로 확인해 주세요.')
    else:
        lines.append('기록 충돌 차단: 현재 저장된 차단 없음 · 실제 거래 검증 완료를 뜻하지 않아요.')
    lines.append(f'결과 미확인 {unknown}건 · 처리 중 {processing}건 · 내용 확인/전송 대기 {pending}건')
    if halted:
        lines.append('사용자가 요청한 모의 긴급중지: 유지 중 · 새 조회 뒤 /모의재개로 별도 확인')
    lines.append(f'새 조회 대조 대기 {waiting}건 · 당일 최종 취소 대조 대기 {cancellations}건')
    if waiting: lines.append('접수 기록과 새 조회를 아직 맞추지 못해 새 주문·정정을 차단해요.')
    if cancellations: lines.append('취소 접수는 완료가 아니에요. 당일 새 주문·정정을 계속 차단해요.')
    lines.append('PC의 당일 주문 허용·시간: '+('현재 허용' if armed and open_now else '잠금 또는 거래 시간 밖'))
    lines.extend(['/잔고 → /잔고결과 → /주문상태로 최신 관측을 확인해 주세요.',
                  '모의 HTS/MTS에서 주문·체결·잔고를 따로 확인해야 해요.',
                  '이 명령은 조회·주문 전송·차단 해제를 실행하지 않아요.'])
    return '\n'.join(lines)
