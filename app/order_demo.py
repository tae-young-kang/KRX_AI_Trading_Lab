"""Run with python -m app.order_demo. Entirely offline, temporary fake money."""
import json
import tempfile
from datetime import date, datetime
from pathlib import Path

from app.order_engine import OrderEngine, OrderRequest
from app.simulated_broker import SimulatedBroker
from app.trading_clock import KST, TradingCalendar


def run_order_demo():
    with tempfile.TemporaryDirectory(prefix='krx-order-demo-') as folder:
        root = Path(folder)
        broker = SimulatedBroker(root/'fake.db', 100000)
        calendar = TradingCalendar({date(2026, 10, 19)})
        clock = lambda: datetime(2026, 10, 19, 10, tzinfo=KST)
        engine = OrderEngine(root/'journal.db', broker, calendar, clock)
        steps = []
        order = engine.submit('buy', OrderRequest('900001', 'BUY', 5, 1000))
        steps.append('매수 접수: 5주 주문, 체결은 아직 0주')
        broker.fill(order['id'], 2, 900)
        engine.reconcile(order['id'])
        steps.append('일부 체결: 2주만 샀으므로 1,800원 차감')
        engine.amend('amend', order['id'], 4, 950)
        engine.cancel('cancel', order['id'])
        steps.append('총 주문을 4주로 바꾼 뒤 남은 2주 취소: 보유 2주 유지')
        sell = engine.submit('sell', OrderRequest('900001', 'SELL', 2, 1100))
        broker.fill(sell['id'], 2, 1100)
        engine.reconcile(sell['id'])
        steps.append('보유 2주 매도 체결: 2,200원 받음')
        # Restart both sides to demonstrate durable state, not an in-memory reset.
        broker = SimulatedBroker(root/'fake.db')
        engine = OrderEngine(root/'journal.db', broker, calendar, clock)
        engine.recover()
        account = engine.account()
        assert account == broker.account() == {'cash': 100400, 'positions': {}}
        steps.append('재시작 후 두 기록 일치: 연습 잔액 100,400원 / 보유 0주')
        return {'mode': 'OFFLINE_SIMULATED', 'steps': steps,
                'cash': account['cash'], 'positions': account['positions'],
                'notice': '가짜 체결을 직접 넣은 시험입니다. 수수료·세금·슬리피지 없는 예시이며 실제 주문·체결 증명이 아닙니다.'}


if __name__ == '__main__':
    print(json.dumps(run_order_demo(), ensure_ascii=False, indent=2))
