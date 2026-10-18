"""GUI-owned receiver. Starts only after an explicit local user action."""
from pathlib import Path
from PySide6.QtCore import QThread, Signal
from app.order_engine import OrderEngine
from app.simulated_broker import SimulatedBroker
from app.telegram_orders import TelegramOrderService
from app.telegram_receiver import TelegramTransport, receive_once
from app.runtime_lock import ReceiverLock, ReceiverAlreadyRunning


class OfflinePracticeCalendar:
    def is_open(self, moment, venue):
        # Virtual practice session, NOT evidence of a real trading day/session.
        return venue == 'KRX'


class TelegramOrderWorker(QThread):
    status = Signal(str)

    def __init__(self, root, token, user_id, chat_id, catalog, conditions=None, orders_enabled=True, watch_status=None,
                 watch_refresh=None, watch_result=None, watch_reconnect=None, watch_validation=None,
                 watch_orders=None, watch_cancel_orders=None):
        super().__init__()
        self.root, self.token = Path(root), token
        self.user_id, self.chat_id, self.catalog = user_id, chat_id, dict(catalog)
        self.service = None
        self.conditions, self.orders_enabled = conditions, orders_enabled
        self.watch_status = watch_status
        self.watch_refresh, self.watch_result = watch_refresh, watch_result
        self.watch_reconnect = watch_reconnect
        self.watch_validation = watch_validation
        self.watch_orders, self.watch_cancel_orders = watch_orders, watch_cancel_orders

    def stop(self):
        self.requestInterruption()
        if self.service is not None:
            self.service.stop()

    def emergency_stop(self):
        if self.service is None:
            return '아직 수신 준비 중입니다. 수신 중지로 연결을 종료할 수 있습니다.'
        return self.service.safety_text(self.service.emergency_stop())

    def resume_after_review(self):
        if self.service is None:
            return '수신 준비가 끝난 뒤 복구 확인을 진행하세요.'
        return self.service.resume_after_review()

    def run(self):
        transport = None
        owner = ReceiverLock(self.root/'receiver.lock')
        owned = False
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            owner.__enter__()  # before bot IO, engine recovery, or session takeover
            owned = True
            transport = TelegramTransport(self.token)
            identity = transport.identity()
            if self.isInterruptionRequested():
                return
            broker = SimulatedBroker(self.root/'fake_broker.db')
            engine = OrderEngine(self.root/'fake_orders.db', broker, OfflinePracticeCalendar())
            self.service = TelegramOrderService(self.root/'telegram_commands.db', engine,
                                                self.user_id, self.chat_id, identity, self.catalog,
                                                conditions=self.conditions, orders_enabled=self.orders_enabled,
                                                watch_status=self.watch_status, watch_refresh=self.watch_refresh,
                                                watch_result=self.watch_result, watch_reconnect=self.watch_reconnect,
                                                watch_validation=self.watch_validation, watch_orders=self.watch_orders,
                                                watch_cancel_orders=self.watch_cancel_orders)
            report = self.service.recovery_status(startup=True) if self.orders_enabled else None
            self.status.emit('수신 중 · '+(self.service.safety_text(report) if self.orders_enabled else '조건·조회 관리 · 키움 모의 주문은 PC의 별도 당일 허용 필요'))
            last_banner = self.service.safety_banner() if self.orders_enabled else ''
            while not self.isInterruptionRequested():
                receive_once(self.service, transport, self.isInterruptionRequested)
                banner = self.service.safety_banner() if self.orders_enabled else ''
                if banner != last_banner and not self.isInterruptionRequested():
                    self.status.emit('수신 중 · 가짜 서버만 연결\n'+banner)
                    last_banner = banner
                self.msleep(100)
        except ReceiverAlreadyRunning:
            self.status.emit('수신 중지 · 다른 창이나 프로그램에서 주문 연습 수신 중입니다. 먼저 그 창의 수신을 종료하세요.')
        except Exception:
            self.status.emit('수신 중지 · 연결 정보·개인 대화방·다른 봇 수신 프로그램을 확인하세요. 자동 재시작하지 않습니다.')
        finally:
            try:
                if self.service is not None:
                    self.service.stop()
            finally:
                try:
                    if transport is not None:
                        transport.close()
                finally:
                    self.token = ''
                    if owned:
                        owner.__exit__(None, None, None)
