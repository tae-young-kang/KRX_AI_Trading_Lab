"""GUI-owned receiver. Starts only after an explicit local user action."""
from pathlib import Path
from PySide6.QtCore import QThread, Signal
from app.order_engine import OrderEngine
from app.simulated_broker import SimulatedBroker
from app.telegram_orders import TelegramOrderService
from app.telegram_receiver import TelegramTransport, receive_once


class OfflinePracticeCalendar:
    def is_open(self, moment, venue):
        # Virtual practice session, NOT evidence of a real trading day/session.
        return venue == 'KRX'


class TelegramOrderWorker(QThread):
    status = Signal(str)

    def __init__(self, root, token, user_id, chat_id, catalog):
        super().__init__()
        self.root, self.token = Path(root), token
        self.user_id, self.chat_id, self.catalog = user_id, chat_id, dict(catalog)
        self.service = None

    def stop(self):
        self.requestInterruption()
        if self.service is not None:
            self.service.stop()

    def run(self):
        transport = None
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            transport = TelegramTransport(self.token)
            identity = transport.identity()
            if self.isInterruptionRequested():
                return
            broker = SimulatedBroker(self.root/'fake_broker.db')
            engine = OrderEngine(self.root/'fake_orders.db', broker, OfflinePracticeCalendar())
            self.service = TelegramOrderService(self.root/'telegram_commands.db', engine,
                                                self.user_id, self.chat_id, identity, self.catalog)
            self.status.emit('수신 중 · 가짜 서버 주문 연습만 가능 · 실제 돈 사용 안 함')
            while not self.isInterruptionRequested():
                receive_once(self.service, transport, self.isInterruptionRequested)
                self.msleep(100)
        except Exception:
            self.status.emit('수신 중지 · 연결 정보·개인 대화방·다른 봇 수신 프로그램을 확인하세요. 자동 재시작하지 않습니다.')
        finally:
            if self.service is not None:
                self.service.stop()
            if transport is not None:
                transport.close()
            self.token = ''
