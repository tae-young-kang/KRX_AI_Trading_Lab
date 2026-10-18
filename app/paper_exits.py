"""Synthetic practice split sales only. No broker, account, quote or order IO."""
import json
from math import isfinite, fsum, isclose
import re
import uuid
from datetime import datetime


def initialize(db):
    if not db.in_transaction:
        db.execute('BEGIN IMMEDIATE')
    columns={r['name'] for r in db.execute('PRAGMA table_info(paper_trades)')}
    if 'remaining_qty' not in columns:
        db.execute('ALTER TABLE paper_trades ADD COLUMN remaining_qty INTEGER')
        db.execute('UPDATE paper_trades SET remaining_qty=CASE WHEN closed_at IS NULL THEN qty ELSE 0 END')
    if 'realized_pnl' not in columns:
        db.execute('ALTER TABLE paper_trades ADD COLUMN realized_pnl REAL DEFAULT 0')
        db.execute('UPDATE paper_trades SET realized_pnl=pnl WHERE closed_at IS NOT NULL')
    db.execute('''CREATE TABLE IF NOT EXISTS paper_exits (
        id INTEGER PRIMARY KEY, trade_id INTEGER NOT NULL, request_key TEXT NOT NULL UNIQUE,
        request_json TEXT NOT NULL, sold_at TEXT NOT NULL, qty INTEGER NOT NULL,
        reference REAL NOT NULL, fill REAL NOT NULL, sell_fee REAL NOT NULL, sell_tax REAL NOT NULL,
        allocated_cost REAL NOT NULL, pnl REAL NOT NULL, costs_json TEXT NOT NULL, reason TEXT NOT NULL)''')
    db.execute('CREATE INDEX IF NOT EXISTS ix_paper_exit_trade ON paper_exits(trade_id,id)')


def _finite(value):
    return type(value) in (int,float) and isfinite(value)


def _check_chronology(opened_at, previous, stamp):
    try:
        # Existing app timestamps are local; explicit offsets retain their instant.
        times=[datetime.fromisoformat(value).astimezone() for value in
               [opened_at]+[row['sold_at'] for row in previous]+[stamp]]
        if any(later < earlier for earlier,later in zip(times,times[1:])):
            raise ValueError('time moved backwards')
    except (TypeError, ValueError, OverflowError, OSError):
        raise ValueError('연습 매도 시각이 이전 기록보다 이르거나 잘못됐어요. PC 시각과 기록을 확인해 주세요.') from None


def _check_segments(previous, entry_cost, original):
    """Validate open-position segments before trusting their cost allocations."""
    try:
        keys=('paper_fee_pct','paper_sell_tax_pct','paper_slippage_pct')
        for row in previous:
            costs=json.loads(row['costs_json'])
            if (not isinstance(costs,dict) or set(costs)!=set(keys)
                    or not all(_finite(costs[k]) and 0<=costs[k]<100 for k in keys)
                    or costs[keys[0]]+costs[keys[1]]>=100
                    or type(row['qty']) is not int or not 0<row['qty']<original
                    or not all(_finite(row[k]) for k in
                               ('reference','fill','sell_fee','sell_tax','allocated_cost','pnl'))
                    or row['reference']<=0):
                raise ValueError('invalid segment')
            fill=row['reference']*(1-costs[keys[2]]/100)
            proceeds=fill*row['qty']
            fee=proceeds*costs[keys[0]]/100
            tax=proceeds*costs[keys[1]]/100
            allocated=entry_cost*row['qty']/original
            expected=(fill,fee,tax,allocated,proceeds-fee-tax-allocated)
            actual=(row[k] for k in ('fill','sell_fee','sell_tax','allocated_cost','pnl'))
            if not all(_finite(value) and isclose(value,stored,rel_tol=1e-12,abs_tol=1e-7)
                       for value,stored in zip(expected,actual)):
                raise ValueError('segment mismatch')
    except (TypeError,ValueError,OverflowError,KeyError):
        raise ValueError('이전 분할매도의 비용·원가·손익 기록을 대조해야 해요. 기록을 바꾸지 않았어요.') from None


def sell(db,trade_id,quantity,observed_price,reason,fee_pct,tax_pct,slip_pct,*,stamp,request_key=None):
    if (type(trade_id) is not int or trade_id<=0 or quantity is not None and (type(quantity) is not int or quantity<=0)
            or not _finite(observed_price) or observed_price<=0
            or not all(_finite(v) and 0<=v<100 for v in (fee_pct,tax_pct,slip_pct))
            or fee_pct+tax_pct>=100 or not isinstance(reason,str) or not 1<=len(reason)<=200):
        raise ValueError('연습 매도 수량·가격·비용을 확인해 주세요.')
    key=request_key if request_key is not None else uuid.uuid4().hex
    if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,80}',key):
        raise ValueError('연습 매도 확인 번호를 확인해 주세요.')
    request=json.dumps(dict(trade_id=trade_id,quantity=quantity,price=float(observed_price),reason=reason,
                           fee=float(fee_pct),tax=float(tax_pct),slip=float(slip_pct)),sort_keys=True,allow_nan=False)
    prior=db.execute('SELECT request_json,pnl FROM paper_exits WHERE request_key=?',(key,)).fetchone()
    if prior:
        if prior['request_json']!=request:
            raise ValueError('이미 다른 내용으로 처리한 연습 매도 확인 번호예요.')
        return prior['pnl']
    trade=db.execute('SELECT * FROM paper_trades WHERE id=?',(trade_id,)).fetchone()
    if trade is None or trade['closed_at'] is not None:
        raise ValueError('청산 가능한 가상 포지션이 없습니다.')
    previous=db.execute('SELECT * FROM paper_exits WHERE trade_id=? ORDER BY id',(trade_id,)).fetchall()
    _check_chronology(trade['opened_at'],previous,stamp)
    remaining=trade['remaining_qty']
    original=trade['qty']
    if (type(original) is not int or original<=0
            or not all(_finite(trade[k]) for k in ('buy_fill','buy_fee'))):
        raise ValueError('연습 진입 기록을 대조해야 해요.')
    entry_cost=trade['buy_fill']*original+trade['buy_fee']
    if not _finite(entry_cost) or entry_cost<=0:
        raise ValueError('연습 진입 원가를 대조해야 해요.')
    _check_segments(previous,entry_cost,original)
    if (type(original) is not int or type(remaining) is not int or not 0<remaining<=original
            or sum(r['qty'] for r in previous)!=original-remaining
            or not all(_finite(trade[k]) for k in ('buy_fill','buy_fee','realized_pnl'))
            or trade['buy_fill']<=0 or trade['buy_fee']<0
            or not isclose(fsum(r['pnl'] for r in previous),trade['realized_pnl'],abs_tol=1e-7)):
        raise ValueError('연습 잔량·이전 매도 기록을 대조해야 해요. 기록을 바꾸지 않았어요.')
    quantity=remaining if quantity is None else quantity
    if quantity>remaining:
        raise ValueError('연습 매도 수량이 남은 수량보다 많아요.')
    entry_cost=trade['buy_fill']*original+trade['buy_fee']
    allocated=(entry_cost-fsum(r['allocated_cost'] for r in previous) if quantity==remaining
               else entry_cost*quantity/original)
    fill=observed_price*(1-slip_pct/100)
    proceeds=fill*quantity
    fee,tax=proceeds*fee_pct/100,proceeds*tax_pct/100
    pnl=proceeds-fee-tax-allocated
    cumulative=fsum([r['pnl'] for r in previous]+[pnl])
    if not all(_finite(v) for v in (entry_cost,allocated,fill,fee,tax,pnl,cumulative)) or allocated<=0 or entry_cost<=0:
        raise ValueError('연습 손익 계산 범위를 벗어났어요.')
    costs=dict(paper_fee_pct=float(fee_pct),paper_sell_tax_pct=float(tax_pct),paper_slippage_pct=float(slip_pct))
    costs_json=json.dumps(costs,sort_keys=True,allow_nan=False)
    db.execute('''INSERT INTO paper_exits(trade_id,request_key,request_json,sold_at,qty,reference,
        fill,sell_fee,sell_tax,allocated_cost,pnl,costs_json,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (trade_id,key,request,stamp,quantity,observed_price,fill,fee,tax,allocated,pnl,costs_json,reason))
    db.execute('UPDATE paper_trades SET remaining_qty=?,realized_pnl=? WHERE id=?',
               (remaining-quantity,cumulative,trade_id))
    if quantity==remaining:
        all_costs=[json.loads(r['costs_json']) for r in previous]+[costs]
        final_costs=costs if all(c==costs for c in all_costs) else dict(mixed=True,segments=all_costs)
        close_ref=fsum([r['reference']*r['qty'] for r in previous]+[observed_price*quantity])/original
        close_fill=fsum([r['fill']*r['qty'] for r in previous]+[proceeds])/original
        percent=cumulative/entry_cost*100
        if not all(_finite(v) for v in (close_ref,close_fill,percent)):
            raise ValueError('연습 청산 합계 계산 범위를 벗어났어요.')
        db.execute('''UPDATE paper_trades SET close_ref=?,close_fill=?,close_fee=?,close_tax=?,
            closed_at=?,close_reason=?,pnl=?,pnl_pct=?,exit_settings_json=? WHERE id=?''',
            (close_ref,close_fill,fsum([r['sell_fee'] for r in previous]+[fee]),
             fsum([r['sell_tax'] for r in previous]+[tax]),stamp,reason,cumulative,percent,
             json.dumps(final_costs,sort_keys=True),trade_id))
    return pnl
