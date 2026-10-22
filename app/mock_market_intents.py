"""Owner-thread market preparation into the existing confirmed intent queue."""
import hashlib
import json
import re
import secrets
import uuid
from app.order_engine import connect


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS market_previews(id TEXT PRIMARY KEY,scope TEXT,session TEXT,uid INTEGER,
        requested REAL,expires REAL,state TEXT,payload TEXT,preview TEXT,UNIQUE(scope,session,uid))''')
    db.execute("UPDATE market_previews SET state='CANCELED' WHERE state IN ('PENDING','PREPARING')")


def request(owner,db,text,scope,session,uid,now):
    match=re.fullmatch(r'/(매수|매도) (.{1,100}?) ([1-9][0-9]{0,14})(주|원|만원) 시장가(?: 익절 ([0-9]{1,2}(?:\.[0-9]{1,2})?) 손절 ([0-9]{1,2}(?:\.[0-9]{1,2})?)(?: (자동매도))?)?',text)
    if not match:return None
    if db.execute("SELECT 1 FROM intents WHERE state IN ('WAITING','QUEUED','CLAIMED','UNKNOWN')").fetchone() or db.execute("SELECT 1 FROM market_previews WHERE state IN ('PENDING','PREPARING')").fetchone():
        return '다른 확인·전송·시장가 준비 또는 미확인 요청이 있어요. /주문상태를 먼저 확인해 주세요.'
    side,symbol,number,unit,take,stop,automatic=match.groups()
    from app.mock_auto_exit import policy as buy_policy
    try:values=buy_policy(side,take,stop,automatic)
    except ValueError as error:return str(error)
    if values.get('auto_exit') is True:values['auto_exit_generation']=owner.generation
    if not owner.symbol_lookup:return '시장가 준비에는 오늘 같은 모드에서 확인한 종목 정보가 필요해요. 먼저 테마1 검색을 완료해 주세요.'
    try:
        info=owner.symbol_lookup(symbol,owner.clock())
        if (not isinstance(info,dict) or not isinstance(info.get('code'),str) or not re.fullmatch('[A-Za-z0-9]{6}',info['code'])
                or info.get('market') not in ('KOSPI','KOSDAQ') or not isinstance(info.get('name'),str) or not 1<=len(info['name'])<=100
                or not info['name'].isprintable()
                or (info['code']!=symbol if re.fullmatch('[A-Za-z0-9]{6}',symbol) else info['name']!=symbol)):
            raise ValueError('종목 정보를 확인해 주세요.')
    except ValueError as error:return str(error)
    except Exception:return '시장가 종목 정보를 확인하지 못했어요. 새 검색 후 다시 요청해 주세요.'
    payload={k:info[k] for k in ('code','name','market')}
    payload.update(values)
    value=int(number)*(10000 if unit=='만원' else 1)
    if unit=='주' and value>10:return '모의 시험 한도는 한 주문10주예요.'
    if unit!='주' and value>100000:return '모의 시험 한도는 한 주문10만원이에요.'
    payload.update(side='BUY' if side=='매수' else 'SELL',quantity=value if unit=='주' else None,budget_won=None if unit=='주' else value)
    db.execute('INSERT INTO market_previews VALUES(?,?,?,?,?,?,?,?,NULL)',
        (uuid.uuid4().hex,scope,session,uid,now,now+120,'PENDING',json.dumps(payload,ensure_ascii=False)))
    return f'KRX 모의 시장가 {side}의 최신 잔고·시세를 확인할게요. 아직 주문하지 않았어요.\n계산 후 확인 메시지를 보내요. /주문상태에서도 확인할 수 있어요.'


def prepare(owner,session):
    with connect(owner.path) as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute("SELECT * FROM market_previews WHERE state='PENDING' ORDER BY requested LIMIT 1").fetchone()
        if row is None:return False
        db.execute("UPDATE market_previews SET state='PREPARING' WHERE id=?",(row['id'],))
    try:
        if owner.clock().timestamp()>row['expires'] or owner._canceled(row['scope'],row['session']):raise ValueError()
        info=json.loads(row['payload'])
        side=info.get('side','BUY')
        args={k:info[k] for k in ('code','name','market','quantity','budget_won')}
        if side=='BUY':result=session.preview_market_buy(**args,fee_bps=100)
        elif side=='SELL':result=session.preview_market_sell(**args)
        else:raise ValueError()
        if result['state']!='PREVIEW_ONLY' or result['contract_draft'] is None:raise ValueError()
        target=result['targets'][0];quantity=target['quantity'];upper=target['reference_won']
        if not 1<=quantity<=10 or quantity*upper>100000:raise ValueError()
        now=owner.clock().timestamp()
        if not owner.armed or owner.emergency.event.is_set() or not owner._open() or owner._canceled(row['scope'],row['session']) or now>row['expires']:raise ValueError()
        payload=dict(code=info['code'],side=side,quantity=quantity,limit_price=upper,order_type='MARKET',
            requested_symbol={k:info[k] for k in ('code','name','market')},
            market_reserved_won=result['reserved_won'])
        if side=='BUY':payload['market_fee_bps']=100
        values={key:info[key] for key in ('take_bps','stop_bps','auto_exit','auto_exit_generation') if key in info}
        payload.update(values)
        if info['budget_won'] is not None:payload['requested_won']=info['budget_won']
        token=secrets.token_urlsafe(18)
        detail=(f'상한가와 비용 여유1% 기준 확보액 {result["reserved_won"]:,}원\n'
            '확보액은 예상 체결가나 지정가가 아니에요. 금액 요청은 상한가·비용 기준으로 주수를 버림 계산해요.\n' if side=='BUY' else
            f'매도 가능 {result["available_quantity"]}주 · 현재가 기준 예상 매도금액 {result["estimated_gross_won"]:,}원 (비용 차감 전)\n'
            '금액 요청은 현재가로 주수를 버림 계산해요. 실제 매도금액·실현손익은 체결 후 확인해야 해요.\n')
        label='매수' if side=='BUY' else '매도'
        from app.mock_auto_exit import policy_text
        text=(f'[키움 모의 KRX 시장가 {label} · 실제 돈 사용 안 함]\n{info["name"]} ({info["code"]}) · {quantity}주\n'
            f'현재가 {result["quote_won"]:,}원 · 조회 상한가 {upper:,}원\n'
            +detail+
            '전송 전에 잔고·상한가를 다시 확인하며 수량은 바꾸지 않아요. NXT/시초가 예약 요청이 아니에요.\n'
            +policy_text(values)+
            f'2분 안에 /모의확인 {token}\n철회: /모의철회 {token}')
        with connect(owner.path) as db:
            db.execute('BEGIN IMMEDIATE')
            state=db.execute('SELECT state FROM market_previews WHERE id=?',(row['id'],)).fetchone()
            if state is None or state['state']!='PREPARING' or not owner.armed or owner.emergency.event.is_set():raise ValueError()
            if owner._usage_block(db,payload) or owner._unresolved_cancel(db) or owner._observation_pending(db):raise ValueError()
            db.execute('INSERT INTO intents VALUES(?,?,?,?,?,?,?,?,?)',
                (row['id'],row['scope'],row['session'],row['uid'],hashlib.sha256(token.encode()).hexdigest(),json.dumps(payload,ensure_ascii=False),now+120,'WAITING',owner.day.isoformat()))
            db.execute("UPDATE market_previews SET state='READY',expires=?,preview=? WHERE id=?",(now+120,text,row['id']))
    except Exception:
        with connect(owner.path) as db:
            db.execute("UPDATE market_previews SET state='FAILED',preview=? WHERE id=? AND state='PREPARING'",('시장가 준비를 마치지 못했어요. 최신 시세·상한가·주문 가능 금액·매도 가능 수량·모의 한도를 확인해 주세요. 주문은 보내지 않았어요.',row['id']))
    return True


def notice(db,scope,session,now):
    row=db.execute('''SELECT p.* FROM market_previews p LEFT JOIN intents i ON i.id=p.id
        WHERE p.scope=? AND p.session=? AND
        (p.state='FAILED' OR (p.expires>=? AND p.state='READY' AND i.state='WAITING'))
        AND NOT EXISTS(SELECT 1 FROM result_delivery d WHERE d.id=p.id||':market-preview')
        ORDER BY p.requested LIMIT 1''',(scope,session,now)).fetchone()
    if not row:return None
    key=row['id']+':market-preview'
    db.execute('INSERT INTO result_delivery VALUES(?,?,?,?)',(key,scope,session,'SENDING'))
    return dict(key=key,text=row['preview'])
