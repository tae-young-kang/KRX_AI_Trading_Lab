"""Pure equal-budget preview; no account, broker, quote refresh or executable orders."""
import re


def equal_budget_preview(*, total_won, targets, available_won, fee_bps, buffer_bps):
    """Floor quantities and retain unused cash. A preview never authorizes execution."""
    def money(value):
        return type(value) is int and 0<=value<=10**15
    if (not money(total_won) or total_won<=0
            or available_won is not None and not money(available_won)
            or type(fee_bps) is not int or not 0<=fee_bps<=10000
            or type(buffer_bps) is not int or not 0<=buffer_bps<=10000):
        raise ValueError('총예산·가용 금액·비용과 가격 여유를 확인해 주세요.')
    targets=list(targets)
    if not 1<=len(targets)<=5000:
        raise ValueError('대상 종목을 1개 이상 선택해 주세요.')
    seen=set()
    for target in targets:
        if (not isinstance(target,dict) or set(target)!={'code','name','market','reference_won'}
                or not isinstance(target['code'],str) or not re.fullmatch(r'[A-Za-z0-9]{6}',target['code'])
                or not isinstance(target['name'],str) or not 1<=len(target['name'])<=100
                or target['market'] not in ('KOSPI','KOSDAQ')):
            raise ValueError('대상 종목 정보를 확인해 주세요.')
        if target['code'] in seen:
            raise ValueError('중복된 종목을 제외해 주세요.')
        seen.add(target['code'])
        if target['reference_won'] is not None and (not money(target['reference_won']) or target['reference_won']==0):
            raise ValueError('기준 가격을 확인해 주세요.')
    allocation=total_won//len(targets)
    rows=[]
    for target in targets:
        price=target['reference_won']
        if price is None:
            rows.append(dict(target,allocated_won=allocation,quantity=None,reserved_won=0,state='PRICE_MISSING'))
            continue
        unit=(price*(10000+fee_bps+buffer_bps)+9999)//10000
        quantity=allocation//unit
        rows.append(dict(target,allocated_won=allocation,quantity=quantity,reserved_won=quantity*unit,
                         state='ESTIMATED' if quantity else 'BELOW_ONE_SHARE'))
    reserved=sum(row['reserved_won'] for row in rows)
    state=('FUNDS_UNKNOWN' if available_won is None else 'INSUFFICIENT_FUNDS' if total_won>available_won
           else 'PRICE_MISSING' if any(row['state']=='PRICE_MISSING' for row in rows) else 'PREVIEW_ONLY')
    return dict(state=state,executable=False,total_won=total_won,allocated_each_won=allocation,
                reserved_won=reserved,unreserved_won=total_won-reserved,
                fee_bps=fee_bps,buffer_bps=buffer_bps,targets=rows)
