"""Explicit parent consent -> one protected MOCK market sale in the existing queue."""
from decimal import Decimal
import hashlib
import json
from app.order_engine import connect
from app.kiwoom_mock_contract import ContractError
from app.mock_protection import position_from_capture
from app.protection_plan import validate_policy


def policy(side,take,stop,automatic):
    if take is None:return {}
    if side!='매수':raise ValueError('익절·손절 조건은 매수 요청에만 붙여 주세요.')
    values=dict(take_bps=int(Decimal(take)*100),stop_bps=int(Decimal(stop)*100))
    validate_policy(**values)
    if automatic:values['auto_exit']=True
    return values


def policy_text(values):
    if not values:return ''
    text=f'체결 뒤 조건: 익절 +{values["take_bps"]/100:g}% · 손절 -{values["stop_bps"]/100:g}%\n'
    if values.get('auto_exit') is True:
        return text+('자동매도 허용: 전량 매수 체결·보유 대조 후 조건 도달 시 추가 확인 없이 KRX 모의 시장가 매도 요청을1회 만들어요.\n'
            '당일 PC/모의 연결/수신/주문 허용이 유지돼야 해요. 한도·잔고·중지·장애로 실행되지 않을 수 있어요.\n')
    return text+'이 요청은 감시만 해요. 자동 매도 미연결 · 자동매도 허용 안 함\n'


def exit_key(parent):return 'auto_exit_'+hashlib.sha256(parent.encode()).hexdigest()[:32]


def verify_position(owner,parent,capture,quantity):
    values=json.loads(parent['payload'])
    if (values.get('side')!='BUY' or values.get('auto_exit') is not True
            or values.get('auto_exit_generation')!=owner.generation
            or parent['state']!='ACKNOWLEDGED' or parent['day']!=owner.day.isoformat()
            or owner._canceled(parent['scope'],parent['session']) or not owner.protection.triggered(parent['id'])):
        raise ContractError('자동매도 허용·원매수·조건 도달 근거를 확인해야 해요.')
    receipt=owner.journal.get(parent['id'])
    if not receipt or receipt['state']!='ACKNOWLEDGED' or not receipt['order_no']:raise ContractError('원매수 접수 근거가 없어요.')
    position=position_from_capture(owner.ledger,capture,code=values['code'],buy_orders=(receipt['order_no'],),now=owner.clock())
    if position.filled_quantity!=values['quantity'] or position.sold_quantity or quantity!=position.filled_quantity:
        raise ContractError('전량 매수 체결과 매도 수량을 다시 확인해야 해요.')
    return values


def enqueue(owner,parent,result,capture,upper):
    """Called only after a newly persisted trigger; never rearms or retries."""
    original=json.loads(parent['payload'])
    if original.get('auto_exit') is not True or not result.get('new_trigger'):return None
    decision=result['decision'];quantity=decision['quantity'];now=owner.clock()
    payload=dict(code=original['code'],side='SELL',quantity=quantity,limit_price=upper.price_won,
        order_type='MARKET',market_reserved_won=0,protection_parent=parent['id'],protection_reason=decision['state'])
    key=exit_key(parent['id'])
    state='QUEUED'
    try:
        if (not owner.armed or not owner._open() or owner.emergency.event.is_set() or owner._latched
                or upper.mode!='MOCK' or upper.code!=original['code'] or upper.venue!='KRX' or upper.day!=owner.day
                or not 0<=(now-upper.observed_at).total_seconds()<=5 or decision['state'] not in ('TAKE_PROFIT','STOP_LOSS')):
            raise ContractError('보호 매도 근거 확인 필요')
        verify_position(owner,parent,capture,quantity)
        if not 1<=quantity<=10 or quantity*upper.price_won>100000:raise ContractError('모의 한도 초과')
        with connect(owner.path) as db:
            db.execute('BEGIN')
            limit_reason=owner._usage_block(db,payload)
            if limit_reason:raise ContractError(limit_reason)
            if (owner._observation_pending(db) or owner._unresolved_cancel(db)
                    or db.execute("SELECT 1 FROM intents WHERE state IN ('WAITING','QUEUED','CLAIMED','UNKNOWN')").fetchone()
                    or db.execute("SELECT 1 FROM market_previews WHERE state IN ('PENDING','PREPARING')").fetchone()):
                raise ContractError('다른 요청·한도·대조 확인 필요')
    except (ContractError,ValueError,TypeError) as error:
        state='FAILED'
        payload['protection_block_reason']=str(error)[:240]
    with connect(owner.path) as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT 1 FROM intents WHERE id=?',(key,)).fetchone():return 'ALREADY_RECORDED'
        if owner.emergency.event.is_set() or not owner.armed or owner._canceled(parent['scope'],parent['session']):state='CANCELED'
        uid=db.execute('SELECT COALESCE(MIN(uid),0)-1 FROM intents WHERE uid<0').fetchone()[0]
        db.execute('INSERT INTO intents VALUES(?,?,?,?,?,?,?,?,?)',
            (key,parent['scope'],parent['session'],uid,'',json.dumps(payload),now.timestamp()+30,state,owner.day.isoformat()))
    return state


def validate_dispatch(owner,row,payload,capture):
    with connect(owner.path) as db:
        parent=db.execute('SELECT * FROM intents WHERE id=? AND scope=? AND session=?',
            (payload['protection_parent'],row['scope'],row['session'])).fetchone()
    if parent is None or row['id']!=exit_key(parent['id']) or payload.get('side')!='SELL' or payload.get('order_type')!='MARKET':
        raise ContractError('보호 매도 요청 연결이 달라요.')
    values=verify_position(owner,parent,capture,payload['quantity'])
    if payload['code']!=values['code']:raise ContractError('보호 매도 종목이 달라요.')
    with connect(owner.protection.path) as db:
        plan=db.execute('SELECT payload,trigger_json FROM protection_plans WHERE plan_key=?',(parent['id'],)).fetchone()
    policy_values=json.loads(plan['payload']);decision=json.loads(plan['trigger_json'])
    if (decision['state']!=payload['protection_reason'] or decision['quantity']!=payload['quantity']
            or policy_values['take_bps']!=values['take_bps'] or policy_values['stop_bps']!=values['stop_bps']):
        raise ContractError('보호 매도 조건 근거가 달라요.')
