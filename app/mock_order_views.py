"""Bounded, scoped MOCK lists. No broker calls or order state changes."""
import json
import re
import sqlite3
from contextlib import closing

from app.order_engine import connect
from app.trading_clock import KST
from app.kiwoom_mock_contract import ContractError


SIZE = 5
LABELS = {
    'WAITING': '내용 확인 대기', 'QUEUED': '전송 대기', 'CLAIMED': '처리 중',
    'UNKNOWN': '결과 미확인 · 재주문 금지', 'ACKNOWLEDGED': '접수 응답 확인',
    'REJECTED': '증권사 거절', 'FAILED': '전송 전 확인 실패',
    'CANCELED': '확인 철회 또는 연결 중지', 'EXPIRED': '확인 시간 만료',
}


def _page(text):
    match = re.fullmatch(r'/(?:주문목록|미체결)(?: ([1-9][0-9]{0,2}))?', text)
    return int(match[1] or 1) if match else None


def _receipts(owner, keys):
    path = owner.root/'dispatch.db'
    if not path.exists():
        return {}
    # URI read-only: viewing history must not initialize or repair the journal.
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        return {key: dict(row) for key in keys if (row := db.execute(
            'SELECT order_no,trade_day,state FROM mock_dispatch WHERE request_key=?', (key,)
        ).fetchone()) is not None}


def _number(receipt, day):
    if (receipt and receipt['trade_day'] == day and receipt['state'] == 'ACKNOWLEDGED'
            and re.fullmatch(r'[0-9]{7}', receipt['order_no'] or '')):
        return receipt['order_no']
    return None


def render(owner, text, scope, now):
    page = _page(text)
    if page is None:
        return '사용법: /주문목록 또는 /미체결\n다음 페이지 예: /주문목록 2'
    pending = text.split(' ', 1)[0] == '/미체결'
    with connect(owner.path) as db:
        db.execute('BEGIN')
        if pending:
            return _pending(owner, db, scope, now, page)
        total = db.execute('SELECT COUNT(*) FROM intents WHERE scope=?', (scope,)).fetchone()[0]
        pages = max(1, (total+SIZE-1)//SIZE)
        if page > pages:
            return f'주문목록은 {pages}페이지까지 있어요. /주문목록 {pages}'
        rows = db.execute('''SELECT i.*,o.checked_at FROM intents i
            LEFT JOIN observations o ON o.id=i.id WHERE i.scope=?
            ORDER BY i.rowid DESC LIMIT ? OFFSET ?''', (scope, SIZE, (page-1)*SIZE)).fetchall()
        receipts = _receipts(owner, [r['id'] for r in rows])
        lines = [f'[키움 모의 주문목록 {page}/{pages} · 실제 돈 사용 안 함]',
                 '본인 요청만 · 최근 순 · 로컬 기록(최신 증권사 조회 아님)']
        for row in rows:
            payload = json.loads(row['payload'])
            action = {'AMEND': '정정', 'CANCEL': '취소'}.get(payload.get('action'),
                     '매수' if payload['side'] == 'BUY' else '매도')
            number = _number(receipts.get(row['id']), row['day'])
            price_label=('시장가 · 예산 상한가 ' if payload.get('order_type')=='MARKET' else '지정가 ')
            if payload.get('order_type')=='MARKET' and payload.get('side')=='SELL':price_label='시장가 · 시험 한도 기준 상한가 '
            lines.append(f"{row['day']} {action} · {payload['code']} · {payload['quantity']}주 · {price_label}{payload['limit_price']:,}원")
            lines.append(LABELS.get(row['state'], '상태 확인 필요') +
                         (f' · 주문번호 {number}' if number else ' · 접수 번호 없음'))
            if payload.get('action') == 'CANCEL' and row['state'] == 'ACKNOWLEDGED':
                lines.append('최종 취소 완료는 미확인')
            if row['checked_at']:
                lines.append('마지막 관측 '+row['checked_at'])
        if not rows:
            lines.append('주문 기록이 없어요.')
        if page < pages:
            lines.append(f'다음: /주문목록 {page+1}')
        lines.append('/잔고: 새 조회 · /미체결: 최근 남은 주문 · 접수는 체결 완료가 아니에요.')
        return '\n'.join(lines)


def _pending(owner, db, scope, now, page):
    capture = owner._capture
    if (capture is None or owner.journal is None
            or not 0 <= now-capture.finished_at.timestamp() <= 30):
        return '미체결 정보가 없거나 오래됐어요. /잔고 조회를 끝내고 30초 안에 /미체결을 요청해 주세요.'
    transport = owner.journal.transport
    try:
        transport.ready()
        if (capture.profile_id != owner.profile or capture.profile_id != transport.profile_id
                or capture.auth_generation != owner.generation
                or capture.auth_generation != transport.auth_generation
                or capture.observations.account_tag != owner.account_tag
                or capture.observations.day != owner.day or transport.current_day != owner.day):
            raise ContractError('context')
    except ContractError:
        return '현재 모의 연결과 조회가 달라요. 집 PC에서 연결을 확인하고 /잔고로 새 조회해 주세요.'
    items = []
    omitted = 0
    for number, code, quantity, remaining in capture.observations.unfilled:
        try:
            row, receipt = owner._target(db, scope, number)
            payload = json.loads(row['payload'])
            if payload['code'] != code or payload['quantity'] != quantity:
                raise ContractError('shape')
            items.append(f"주문번호 {receipt['order_no']} · {code} · 남은 {remaining}주")
        except ContractError:
            omitted += 1
    pages = max(1, (len(items)+SIZE-1)//SIZE)
    if page > pages:
        return f'미체결 목록은 {pages}페이지까지 있어요. /미체결 {pages}'
    lines = [f'[키움 모의 미체결 {page}/{pages} · 실제 돈 사용 안 함]',
             '조회 시각 '+capture.finished_at.astimezone(KST).strftime('%Y-%m-%d %H:%M:%S'),
             '본인이 이 프로그램에서 접수한 오늘의 최신 주문만 표시해요.']
    lines += items[(page-1)*SIZE:page*SIZE] or ['표시할 미체결 주문이 없어요. 체결·최종 취소 완료를 증명하지는 않아요.']
    if omitted:
        lines.append('목록에서 제외한 외부·이전 번호 또는 대조 필요 주문이 있어요. 모의 HTS에서 확인해 주세요.')
    if page < pages:
        lines.append(f'다음: /미체결 {page+1}')
    lines.append('조회 후 잔량은 바뀔 수 있어요. 정정·취소는 별도 확인과 새 대조가 필요해요.\n/주문점검: 현재 주문 차단 이유')
    return '\n'.join(lines)
