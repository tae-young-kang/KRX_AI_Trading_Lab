"""Read-only same-day MOCK evidence bridge. Never submits or authorizes orders."""
from dataclasses import asdict
from datetime import datetime
import json
from app.kiwoom_mock_contract import ContractError
from app.kiwoom_mock_ledger import MockFillLedger, _json
from app.kiwoom_mock_review import CapturedObservations, order_number, stock_code
from app.order_engine import connect
from app.protection_plan import FilledPosition
from app.trading_clock import KST


def position_from_capture(ledger, capture, *, code, buy_orders, sell_orders=(), now):
    if type(ledger) is not MockFillLedger or type(capture) is not CapturedObservations:
        raise ContractError('모의 체결 장부와 시각이 확인된 조회 묶음이 필요해요.')
    observation=capture.observations
    stamps=(now,capture.started_at,capture.finished_at)
    if (any(not isinstance(stamp,datetime) or stamp.tzinfo is None or stamp.utcoffset() is None for stamp in stamps)
            or not 0<=(now-capture.started_at).total_seconds()<=30
            or not capture.started_at<=capture.finished_at<=now
            or observation.day!=now.astimezone(KST).date()
            or observation.account_tag!=ledger._account_tag):
        raise ContractError('같은 모의 계좌의 오늘 30초 이내 조회가 필요해요.')
    code=stock_code(code)
    buys=tuple(order_number(number) for number in buy_orders)
    sells=tuple(order_number(number) for number in sell_orders)
    if not buys or len(set(buys+sells))!=len(buys+sells):
        raise ContractError('서로 겹치지 않는 매수·매도 주문번호가 필요해요.')
    expected={number:'BUY' for number in buys}|{number:'SELL' for number in sells}
    payload=asdict(observation)
    payload['day']=observation.day.isoformat()
    del payload['account_tag']
    with connect(ledger.path) as db:
        db.execute('BEGIN')
        tag=db.execute('SELECT account_tag FROM fill_meta').fetchone()
        if not tag or tag['account_tag']!=observation.account_tag:
            raise ContractError('영속 장부의 모의 계좌 구분이 달라요.')
        stored=db.execute('SELECT payload FROM fill_snapshots WHERE day=?',(payload['day'],)).fetchone()
        if not stored or stored['payload']!=_json(payload):
            raise ContractError('조회 묶음이 최신 영속 체결 장부와 달라요.')
        rows=db.execute('SELECT payload FROM fill_events WHERE day=?',(payload['day'],)).fetchall()
        if sorted(row['payload'] for row in rows)!=sorted(_json(asdict(item)) for item in observation.executions):
            raise ContractError('영속 체결 행과 조회 묶음이 달라요.')
    fills=[json.loads(row['payload']) for row in rows]
    relevant=[item for item in fills if item['code']==code or item['order_no'] in expected]
    if any(item['code']!=code or expected.get(item['order_no'])!=item['side'] for item in relevant):
        raise ContractError('같은 종목의 체결 중 계획에 연결되지 않은 주문이 있어요.')
    if set(item['order_no'] for item in relevant)!=set(expected):
        raise ContractError('계획의 주문별 체결 근거가 부족해요.')
    buy_qty=sum(item['quantity'] for item in relevant if item['side']=='BUY')
    buy_value=sum(item['quantity']*item['price'] for item in relevant if item['side']=='BUY')
    sold=sum(item['quantity'] for item in relevant if item['side']=='SELL')
    holdings=[row for row in observation.holdings if row[0]==code]
    remaining=buy_qty-sold
    if (remaining<0 or len(holdings)>1 or
            (holdings[0][1] if holdings else 0)!=remaining or
            (holdings[0][2] if holdings else 0)!=remaining):
        raise ContractError('체결 잔량과 보유·매도 가능 수량이 일치하지 않아요.')
    # Unfilled rows lack side attribution here, so even an unfilled buy blocks
    # protection planning instead of guessing that it is safe to add a sale.
    if any(row[1]==code and row[3]>0 for row in observation.unfilled):
        raise ContractError('같은 종목의 미체결 주문이 있어 보호 판단을 보류해요.')
    return FilledPosition('MOCK',code,'KRX',buy_qty,buy_value,sold,reconciled=True)
