"""Durable single-attempt alert delivery boundary. No built-in network client."""
import json
import re
from hashlib import sha256


def destination_scope(bot_id,chat_id):
    if any(type(value) is not int or value<=0 for value in (bot_id,chat_id)):
        raise ValueError('허용한 봇과 개인 대화방을 확인해 주세요.')
    return sha256(f'theme1:{bot_id}:{chat_id}'.encode()).hexdigest()


def delivery_summary(history, *, mode, source=None, destination):
    """Latest run only. No network, retries or original recipient identifiers."""
    with history.connect() as db:
        db.execute('BEGIN')
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='theme_runs'").fetchone():
            return '테마1 판정 기록이 없어요.'
        run=db.execute('SELECT day,source,alert_ids_json FROM theme_runs WHERE mode=?'+
                       (' AND source=?' if source else '')+' ORDER BY id DESC LIMIT 1',
                       (mode,source) if source else (mode,)).fetchone()
        if run is None:
            return '이 모드·출처의 테마1 판정 기록이 없어요.'
        ids=json.loads(run['alert_ids_json'])
        states={}
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='theme_deliveries'").fetchone():
            states={row['alert_id']:row['state'] for row in db.execute(
                'SELECT alert_id,state FROM theme_deliveries WHERE destination=?',(destination,))}
        sent=sum(states.get(value)=='SENT' for value in ids)
        unknown=sum(states.get(value) in ('SENDING','UNKNOWN') for value in ids)
        return (f'[테마1 알림 전송 기록] {run["day"]} · 자료 {run["source"]}\n감지 {len(ids)}개 · 발송 응답 확인 {sent}개 · '
                f'결과 미확인 {unknown}개 · 미시도 {len(ids)-sent-unknown}개\n'
                '미확인은 자동 재전송하지 않아요. 메시지를 읽었는지·주문했는지는 이 기록으로 알 수 없어요.')


def deliver_one(history, *, alert_id, mode, source, destination, send):
    """Injected sender returns positive Telegram message id; uncertainty never retries.

    destination is an opaque SHA-256 scope, never a token/chat id. Production
    caller must bind it to its explicitly configured, authorized recipient.
    """
    if (type(alert_id) is not int or alert_id<=0 or mode not in ('DEMO','MOCK','REAL_QUOTES')
            or source not in ('DEMO','KRX','CSV_USER') or (mode=='DEMO')!=(source=='DEMO')
            or not isinstance(destination,str) or not re.fullmatch('[a-f0-9]{64}',destination)
            or not callable(send)):
        raise ValueError('알림 대상과 자료 모드를 확인해 주세요.')
    with history.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('''CREATE TABLE IF NOT EXISTS theme_deliveries (
            alert_id INTEGER NOT NULL, destination TEXT NOT NULL, state TEXT NOT NULL,
            message_id INTEGER, PRIMARY KEY(alert_id,destination))''')
        prior=db.execute('SELECT state FROM theme_deliveries WHERE alert_id=? AND destination=?',
                         (alert_id,destination)).fetchone()
        if prior:
            return prior['state'] if prior['state']!='SENDING' else 'UNKNOWN'
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='theme_runs'").fetchone():
            raise ValueError('테마1 판정 기록이 없어요.')
        run=db.execute('SELECT alert_ids_json FROM theme_runs WHERE mode=? AND source=? ORDER BY id DESC LIMIT 1',
                       (mode,source)).fetchone()
        if run is None or alert_id not in json.loads(run['alert_ids_json']):
            raise ValueError('최신 테마1 감지 목록에 없는 알림이에요.')
        alert=db.execute('SELECT message FROM theme_alerts WHERE id=? AND mode=? AND source=?',
                         (alert_id,mode,source)).fetchone()
        if alert is None or len(alert['message'])>4000:
            raise ValueError('알림 내용을 확인해 주세요.')
        message=alert['message']
        db.execute('INSERT INTO theme_deliveries VALUES(?,?,?,NULL)',(alert_id,destination,'SENDING'))
    # Commit before crossing IO boundary. A crash at either side blocks retry.
    try:
        receipt=send(message)
        if type(receipt) is not int or receipt<=0:
            raise ValueError('missing receipt')
        state='SENT'
    except Exception:
        receipt=None
        state='UNKNOWN'
    with history.connect() as db:
        db.execute('UPDATE theme_deliveries SET state=?,message_id=? WHERE alert_id=? AND destination=? AND state=?',
                   (state,receipt,alert_id,destination,'SENDING'))
    return state
