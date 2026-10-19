"""MOCK market-buy budget evidence; no permission, transport or queue mutation."""
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


def preview_buy(capture,quote,upper,*,code,name,market,profile_id,auth_generation,account_tag,
                now,fee_bps,quantity=None,budget_won=None):
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
