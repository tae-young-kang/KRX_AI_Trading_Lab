"""One-attempt result notices in the existing MOCK intent store."""
import json
import re
from decimal import Decimal
from app.order_engine import connect


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS result_delivery (
        id TEXT PRIMARY KEY, scope TEXT, session TEXT, state TEXT)''')
    db.execute("UPDATE result_delivery SET state='UNKNOWN' WHERE state='SENDING'")
    db.execute('CREATE TABLE IF NOT EXISTS fill_evidence (id TEXT PRIMARY KEY,filled INTEGER,gross INTEGER)')


def delivery(path, action, *, scope, session, key=None, delivered=False):
    """Claim before IO; errors, restart and missing receipt never auto retry."""
    with connect(path) as db:
        db.execute('BEGIN IMMEDIATE')
        if action=='finish':
            db.execute("UPDATE result_delivery SET state=? WHERE id=? AND scope=? AND session=? AND state='SENDING'",
                ('SENT' if delivered is True else 'UNKNOWN',key,scope,session))
            return
        if action!='claim':raise ValueError('알림 처리 종류를 확인해 주세요.')
        row=db.execute('''SELECT i.* FROM intents i WHERE i.scope=? AND i.session=?
            AND i.state IN ('ACKNOWLEDGED','REJECTED','FAILED','UNKNOWN')
            AND NOT EXISTS (SELECT 1 FROM result_delivery d WHERE d.id=i.id)
            ORDER BY i.rowid LIMIT 1''',(scope,session)).fetchone()
        if row is None:return fill_delivery(db,scope,session)
        payload=json.loads(row['payload'])
        code=payload.get('code')
        size=payload.get('quantity')
        if not isinstance(code,str) or not re.fullmatch(r'[A-Za-z0-9]{6}',code) or type(size) is not int or size<=0:
            raise ValueError('주문 알림 기록 확인이 필요해요.')
        labels={'ACKNOWLEDGED':'증권사 접수 응답 확인 · 체결 완료는 아니에요.',
                'REJECTED':'증권사가 요청을 거절했어요.',
                'FAILED':'전송 전 확인에 실패했어요. 새 조회와 새 내용 확인이 필요해요.',
                'UNKNOWN':'처리 결과 미확인 · 중복 주문하지 마세요. 자동 재전송하지 않아요.'}
        actions={'NEW':'새 주문','AMEND':'정정 요청','CANCEL':'취소 요청'}
        side={'BUY':'매수','SELL':'매도'}.get(payload.get('side'),'방향 확인 필요')
        action_name=actions.get(payload.get('action','NEW'),'요청')
        text=(f'[키움 모의투자 · 주문 처리 결과]\n실제 돈 사용 안 함\n'
              f'{code} · {side} {size:,}주 · {action_name}\n{labels[row["state"]]}\n'
              '/주문상태로 상세 확인 · /잔고로 새 조회')
        if payload.get('action')=='CANCEL' and row['state']=='ACKNOWLEDGED':
            text+='\n취소 접수 응답이며 최종 취소 완료는 아직 미확인이에요.'
        if row['state']=='FAILED' and payload.get('protection_block_reason'):
            text+='\n보호 매도 보류: '+payload['protection_block_reason']
        db.execute('INSERT INTO result_delivery VALUES(?,?,?,?)',(row['id'],scope,session,'SENDING'))
        return dict(key=row['id'],text=text)


def fill_delivery(db,scope,session):
    row=db.execute('''SELECT i.id,i.payload,o.filled,o.quantity,o.checked_at,o.state,f.gross
        FROM intents i JOIN observations o ON o.id=i.id JOIN fill_evidence f ON f.id=i.id
        WHERE i.scope=? AND i.session=? AND i.state='ACKNOWLEDGED'
        AND o.filled>0 AND o.filled=f.filled AND o.filled<=o.quantity
        AND o.state IN ('부분체결·남은 주문 조회','체결 수량 조회 일치 · 비용/결제 별도 확인')
        AND NOT EXISTS (SELECT 1 FROM result_delivery d WHERE d.id=i.id||':fill:'||o.filled)
        ORDER BY i.rowid LIMIT 1''',(scope,session)).fetchone()
    if row is None:return None
    payload=json.loads(row['payload'])
    code=payload.get('code')
    if (not isinstance(code,str) or not re.fullmatch(r'[A-Za-z0-9]{6}',code)
            or type(row['gross']) is not int or row['gross']<=0):
        raise ValueError('체결 알림의 종목·금액 근거를 확인해 주세요.')
    side={'BUY':'매수','SELL':'매도'}.get(payload.get('side'),'방향 확인 필요')
    label='전량 체결 수량 조회 일치' if row['filled']==row['quantity'] else '부분체결 조회'
    average=Decimal(row['gross'])/Decimal(row['filled'])
    text=(f'[키움 모의투자 · {label}]\n실제 돈 사용 안 함\n'
          f'{code} · {side} · 조회된 체결 {row["filled"]:,}/{row["quantity"]:,}주\n'
          f'누적 체결금액 {row["gross"]:,}원 · 평균 체결가 약 {average:,.2f}원\n'
          f'조회 시각 {row["checked_at"]} (한국 시각)\n'
          '수수료·세금 차감 전 금액이에요. 실현손익·결제 완료는 별도 확인이 필요해요.\n'
          '익절/손절 자동 매도는 아직 연결되지 않았어요. /주문상태 · /잔고')
    key=row['id']+':fill:'+str(row['filled'])
    db.execute('INSERT INTO result_delivery VALUES(?,?,?,?)',(key,scope,session,'SENDING'))
    return dict(key=key,text=text)


def protection_delivery(path,protection,*,scope,session):
    """Notify a stored trigger, not a sale or a new permission to sell."""
    with connect(protection.path) as db:
        plans=db.execute("SELECT * FROM protection_plans WHERE state='TRIGGERED' ORDER BY rowid").fetchall()
    with connect(path) as db:
        db.execute('BEGIN IMMEDIATE')
        for plan in plans:
            key=plan['plan_key']+':protection'
            if db.execute('SELECT 1 FROM result_delivery WHERE id=?',(key,)).fetchone():continue
            row=db.execute("SELECT payload FROM intents WHERE id=? AND scope=? AND session=? AND state='ACKNOWLEDGED'",
                           (plan['plan_key'],scope,session)).fetchone()
            if row is None:continue
            cfg=json.loads(plan['payload'])
            request=json.loads(row['payload'])
            decision=json.loads(plan['trigger_json'])
            code=cfg.get('code')
            quantity=decision.get('quantity')
            if (request.get('code')!=code or not isinstance(code,str) or not re.fullmatch('[A-Za-z0-9]{6}',code)
                    or decision.get('state') not in ('TAKE_PROFIT','STOP_LOSS')
                    or type(quantity) is not int or quantity<=0):continue
            label='익절' if decision['state']=='TAKE_PROFIT' else '손절'
            text=(f'[키움 모의투자 · {label} 조건 도달]\n실제 돈 사용 안 함\n'
                  f'{code} · 당시 확인된 보유 {quantity:,}주\n'
                  f'설정: 익절 +{cfg["take_bps"]/100:.2f}% · 손절 -{cfg["stop_bps"]/100:.2f}%\n'
                  f'관측 시각 {plan["last_at"]}\n'
                  '체결 평균가 기준 조건이에요. 매도 주문은 아직 보내지 않았어요.\n'
                  '현재 자동 매도 전송은 미연결입니다. /보호상태 · /잔고로 확인하세요.')
            if request.get('auto_exit') is True:
                from app.mock_auto_exit import exit_key
                child=db.execute('SELECT state FROM intents WHERE id=? AND scope=? AND session=?',
                    (exit_key(plan['plan_key']),scope,session)).fetchone()
                labels={'QUEUED':'시장가 매도 전송 대기','CLAIMED':'시장가 매도 처리 중',
                    'ACKNOWLEDGED':'시장가 매도 접수 응답 확인 · 체결은 별도',
                    'FAILED':'시장가 매도 전송 전 차단 · 자동 재시도 안 함',
                    'CANCELED':'시장가 매도 요청 취소됨','EXPIRED':'시장가 매도 요청 만료됨',
                    'UNKNOWN':'시장가 매도 결과 미확인 · 재전송 금지'}
                status=labels.get(child['state'],'요청 기록 확인 필요') if child else '자동 매도 요청 기록 없음 · 처리 성공 미확인'
                text=(f'[키움 모의투자 · {label} 조건 도달]\n실제 돈 사용 안 함\n{code} · 당시 확인 수량 {quantity}주\n'
                    f'체결 평균가 기준 · 익절 +{cfg["take_bps"]/100:.2f}% / 손절 -{cfg["stop_bps"]/100:.2f}%\n'
                    f'관측 시각 {plan["last_at"]}\n자동매도 허용 요청: {status}\n'
                    '관측·요청·접수·체결은 서로 달라요. /주문목록 · /주문상태 · /잔고로 확인하세요.')
            db.execute('INSERT INTO result_delivery VALUES(?,?,?,?)',(key,scope,session,'SENDING'))
            return dict(key=key,text=text)
