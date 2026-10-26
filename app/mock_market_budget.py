"""MOCK market-order evidence; no permission, transport or queue mutation."""
from dataclasses import dataclass
from datetime import datetime,date
import re
from app.budget_preview import equal_budget_preview
from app.kiwoom_mock_contract import ContractError,market_order_plan
from app.kiwoom_mock_review import CapturedObservations,Observations
from app.kiwoom_mock_status import money_view
from app.protection_plan import ObservedPrice
from app.trading_clock import KST


@dataclass(frozen=True)
class UpperPrice:
    mode:str
    code:str
    venue:str
    day:date
    price_won:int
    observed_at:datetime


def _validate_evidence(capture,quote,upper,*,code,profile_id,auth_generation,account_tag,now,fee_bps):
    """Use broker upper-price evidence, not a fabricated current-price buffer.

    A final caller must obtain this evidence from independent live MOCK reads,
    recheck it after user confirmation, and separately authorize dispatch.
    """
    if (type(capture) is not CapturedObservations or type(capture.observations) is not Observations
            or type(quote) is not ObservedPrice or type(upper) is not UpperPrice):
        raise ContractError('같은 모의 연결의 잔고·시세·상한가격 조회가 필요해요.')
    identities=((profile_id,32),(auth_generation,32),(account_tag,64))
    if any(not isinstance(value,str) or not re.fullmatch('[a-f0-9]{'+str(size)+'}',value) for value,size in identities):
        raise ContractError('모의 연결 확인 정보를 다시 확인해 주세요.')
    if (capture.profile_id!=profile_id or capture.auth_generation!=auth_generation
            or capture.observations.account_tag!=account_tag):
        raise ContractError('모의 계좌 또는 연결이 바뀌었어요. 다시 조회해 주세요.')
    stamps=(now,capture.started_at,capture.finished_at,quote.quote_at,quote.fetched_at,upper.observed_at)
    if any(not isinstance(stamp,datetime) or stamp.tzinfo is None or stamp.utcoffset() is None for stamp in stamps):
        raise ContractError('시간대가 확인된 조회 시각이 필요해요.')
    day=now.astimezone(KST).date()
    if (not capture.started_at<=capture.finished_at<=now
            or not 0<=(now-capture.started_at).total_seconds()<=30
            or not quote.quote_at<=quote.fetched_at<=now
            or not 0<=(now-quote.quote_at).total_seconds()<=5
            or not 0<=(now-upper.observed_at).total_seconds()<=5
            or any(stamp.astimezone(KST).date()!=day for stamp in stamps)
            or capture.observations.day!=day or type(upper.day) is not date or upper.day!=day):
        raise ContractError('오늘의 최근 잔고·5초 이내 시세와 상한가격을 다시 확인해 주세요.')
    if (not isinstance(code,str) or not re.fullmatch(r'[A-Za-z0-9]{6}',code)
            or quote.mode!='MOCK' or upper.mode!='MOCK' or quote.venue!='KRX' or upper.venue!='KRX'
            or quote.code!=code or upper.code!=code):
        raise ContractError('같은 종목의 KRX 모의 시세가 필요해요.')
    if (any(type(value) is not int or not 0<value<10**12 for value in (quote.price_won,upper.price_won))
            or quote.price_won>upper.price_won or type(fee_bps) is not int or not 0<=fee_bps<=10000):
        raise ContractError('시세·상한가격·비용 기준을 확인해 주세요.')
    if capture.observations.unfilled:
        raise ContractError('미체결 주문 대조가 남아 있어 시장가 예산 계산을 보류해요.')


def preview_buy(capture,quote,upper,*,code,name,market,profile_id,auth_generation,account_tag,
                now,fee_bps,quantity=None,budget_won=None):
    _validate_evidence(capture,quote,upper,code=code,profile_id=profile_id,auth_generation=auth_generation,
        account_tag=account_tag,now=now,fee_bps=fee_bps)
    available=money_view(capture.observations,{}).order_money
    if available is None:raise ContractError('지금 주문에 쓸 수 있는 돈을 확인하지 못했어요.')
    if (quantity is None)==(budget_won is None):raise ContractError('수량 또는 금액 중 하나만 지정해 주세요.')
    unit=(upper.price_won*(10000+fee_bps)+9999)//10000
    if quantity is not None:
        if type(quantity) is not int or not 1<=quantity<10**12:raise ContractError('수량은 허용 범위의 정수로 입력해 주세요.')
        total=quantity*unit
    else:total=budget_won
    result=equal_budget_preview(total_won=total,targets=[dict(code=code,name=name,market=market,reference_won=upper.price_won)],
        available_won=available,fee_bps=fee_bps,buffer_bps=0)
    result.update(mode='MOCK',quote_won=quote.price_won,quantity_basis='BROKER_UPPER_PRICE',
        price_asof=upper.observed_at.isoformat(),available_won=available,executable=False)
    count=result['targets'][0]['quantity']
    if count==0 and result['state']!='INSUFFICIENT_FUNDS':result['state']='BELOW_ONE_SHARE'
    result['contract_draft']=(market_order_plan(code=code,side='BUY',quantity=count)
                              if result['state']=='PREVIEW_ONLY' and count else None)
    return result


def preview_sell(capture,quote,upper,*,code,name,market,profile_id,auth_generation,account_tag,
                 now,quantity=None,budget_won=None):
    """Freeze shares from the requested count or current-quote amount, never short."""
    _validate_evidence(capture,quote,upper,code=code,profile_id=profile_id,auth_generation=auth_generation,
        account_tag=account_tag,now=now,fee_bps=0)
    if market not in ('KOSPI','KOSDAQ') or not isinstance(name,str) or not 1<=len(name)<=100:
        raise ContractError('시장과 종목명을 확인해 주세요.')
    if (quantity is None)==(budget_won is None):raise ContractError('수량 또는 금액 중 하나만 지정해 주세요.')
    value=quantity if quantity is not None else budget_won
    if type(value) is not int or not 0<value<10**15:raise ContractError('매도 수량·금액을 확인해 주세요.')
    count=value if quantity is not None else value//quote.price_won
    holdings=capture.observations.holdings
    if any(not isinstance(row,tuple) or len(row)!=3 or any(type(v) is not int or v<0 for v in row[1:])
           or row[2]>row[1] for row in holdings):raise ContractError('매도 가능 수량 근거를 확인해 주세요.')
    rows=[row for row in holdings if row[0]==code]
    if len(rows)>1:raise ContractError('중복 보유 종목을 확인해 주세요.')
    available=rows[0][2] if rows else 0
    state='BELOW_ONE_SHARE' if count==0 else 'INSUFFICIENT_SHARES' if count>available else 'PREVIEW_ONLY'
    return dict(state=state,mode='MOCK',executable=False,quote_won=quote.price_won,
        available_quantity=available,estimated_gross_won=count*quote.price_won,reserved_won=0,
        quantity_basis='REQUESTED_SHARES' if quantity is not None else 'CURRENT_QUOTE',
        price_asof=quote.quote_at.isoformat(),
        targets=[dict(code=code,name=name,market=market,quantity=count,reference_won=upper.price_won)],
        contract_draft=market_order_plan(code=code,side='SELL',quantity=count) if state=='PREVIEW_ONLY' else None)
