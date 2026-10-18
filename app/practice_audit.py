"""Read-only consistency checks of synthetic split-sale arithmetic, not fill proof."""
from datetime import datetime
from math import isclose,fsum,isfinite
import json


def verify(trade, sales):
    if not sales:
        return  # Old completed entries have no fabricated per-sale evidence.
    def finite(value): return type(value) in (int,float) and isfinite(value)
    def equal(actual,expected):
        if not finite(actual) or not finite(expected) or not isclose(actual,expected,rel_tol=1e-12,abs_tol=1e-7):
            raise ValueError()
    try:
        original=trade['qty']
        if (type(original) is not int or original<=0 or not finite(trade['buy_fill'])
                or not finite(trade['buy_fee']) or trade['buy_fill']<=0 or trade['buy_fee']<0):
            raise ValueError()
        entry=trade['buy_fill']*original+trade['buy_fee']
        remaining=original
        previous_time=datetime.fromisoformat(trade['opened_at']).astimezone()
        allocations=[]
        for sale in sales:
            quantity=sale['qty']
            stamp=datetime.fromisoformat(sale['sold_at']).astimezone()
            if type(quantity) is not int or not 0<quantity<=remaining or stamp<previous_time:
                raise ValueError()
            costs=json.loads(sale['costs_json'])
            keys=('paper_fee_pct','paper_sell_tax_pct','paper_slippage_pct')
            if (not isinstance(costs,dict) or set(costs)!=set(keys)
                    or not all(finite(costs[k]) and 0<=costs[k]<100 for k in keys)
                    or costs[keys[0]]+costs[keys[1]]>=100
                    or not finite(sale['reference']) or sale['reference']<=0):
                raise ValueError()
            fill=sale['reference']*(1-costs[keys[2]]/100)
            proceeds=fill*quantity
            fee=proceeds*costs[keys[0]]/100
            tax=proceeds*costs[keys[1]]/100
            allocated=entry-fsum(allocations) if quantity==remaining else entry*quantity/original
            for key,value in (('fill',fill),('sell_fee',fee),('sell_tax',tax),('allocated_cost',allocated),
                              ('pnl',proceeds-fee-tax-allocated)):
                equal(sale[key],value)
            remaining-=quantity
            allocations.append(allocated)
            previous_time=stamp
        if type(trade['remaining_qty']) is not int or trade['remaining_qty']!=remaining:
            raise ValueError()
        realized=fsum(row['pnl'] for row in sales)
        equal(trade['realized_pnl'],realized)
        if bool(trade['closed_at'])!=(remaining==0):
            raise ValueError()
        if remaining==0:
            equal(trade['pnl'],realized)
            equal(trade['pnl_pct'],realized/entry*100)
            equal(trade['close_fee'],fsum(row['sell_fee'] for row in sales))
            equal(trade['close_tax'],fsum(row['sell_tax'] for row in sales))
            equal(trade['close_fill'],fsum(row['fill']*row['qty'] for row in sales)/original)
            equal(trade['close_ref'],fsum(row['reference']*row['qty'] for row in sales)/original)
            if trade['close_reason']!=sales[-1]['reason']:
                raise ValueError()
            if datetime.fromisoformat(trade['closed_at']).astimezone()!=previous_time:
                raise ValueError()
    except (TypeError,ValueError,OverflowError,KeyError,OSError):
        raise ValueError('연습 매도 장부의 수량·비용·손익·시각을 대조해야 해요. 결산을 확정하지 않았어요.') from None
