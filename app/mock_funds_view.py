"""Fresh read-only MOCK money explanation; never an order authorization."""
from datetime import datetime
from app.trading_clock import KST


FIELDS = ('order_money', 'deposit', 'tomorrow_deposit', 'second_day_deposit', 'unfilled_buy_estimate')


def extract(report):
    values = report.get('funds')
    stamp = report.get('captured_at')
    if (not isinstance(values, dict) or set(values) != set(FIELDS)
            or not isinstance(stamp, datetime) or stamp.tzinfo is None or stamp.utcoffset() is None):
        return None
    if any(type(values[k]) is not int and not (values[k] is None and k in ('order_money','unfilled_buy_estimate'))
           for k in FIELDS):
        return None
    if any(values[k] is not None and values[k] < 0 for k in ('order_money','unfilled_buy_estimate')):
        return None
    return stamp.astimezone(KST), {k: values[k] for k in FIELDS}


def render(saved, *, now, stale):
    head = '[키움 모의 자금 · 실제 돈 사용 안 함]\n'
    if saved is None:
        return head+'자금 상세 조회가 없어요. /잔고 → /잔고결과 완료 뒤 /모의자금을 보내 주세요.'
    stamp, values = saved
    if stale or now.astimezone(KST).date() != stamp.date() or not 0 <= (now-stamp).total_seconds() <= 30:
        return head+'이전 조회는 지금의 돈 상태로 표시할 수 없어요. /잔고 → /잔고결과 완료 뒤 30초 안에 다시 확인해 주세요.'
    available = values['order_money']
    reserve = values['unfilled_buy_estimate']
    lines = [head.rstrip(), f'조회 시각: {stamp:%Y-%m-%d %H:%M:%S}',
             '지금 주문에 쓸 수 있는 돈(조회값): '+('대조 필요' if available is None else f'{available:,}원'),
             f"예수금(조회값): {values['deposit']:,}원",
             f"다음 결제일 예상 예수금(D+1): {values['tomorrow_deposit']:,}원",
             f"그다음 결제일 예상 예수금(D+2): {values['second_day_deposit']:,}원",
             '아직 안 산 주문금액(수수료 별도): '+('대조 필요' if reserve is None else f'{reserve:,}원'),
             '미체결 주문을 모르면 주문 가능 금액을 확정하지 않아요.',
             '예상 예수금을 지금 쓸 돈에 더하거나, 남은 주문금액을 조회된 주문 가능 금액에서 또 빼지 않아요.',
             '조회 후 금액은 바뀔 수 있어요. 비용·예약·결제의 독립 대조는 별도로 필요해요.',
             '이 표시는 주문 허용이 아니에요. /주문점검에서 현재 차단 상태를 확인해 주세요.']
    return '\n'.join(lines)
