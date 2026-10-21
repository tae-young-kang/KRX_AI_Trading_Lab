"""GUI-owned receiver. Starts only after an explicit local user action."""
from pathlib import Path
import time
from PySide6.QtCore import QThread, Signal
from app.order_engine import OrderEngine
from app.simulated_broker import SimulatedBroker
from app.telegram_orders import TelegramOrderService
from app.telegram_receiver import TelegramTransport, TelegramConnectionError, receive_once
from app.runtime_lock import ReceiverLock, ReceiverAlreadyRunning


class OfflinePracticeCalendar:
    def is_open(self, moment, venue):
        # Virtual practice session, NOT evidence of a real trading day/session.
        return venue == 'KRX'


class TelegramOrderWorker(QThread):
    status = Signal(str)

    def __init__(self, root, token, user_id, chat_id, catalog, conditions=None, orders_enabled=True, watch_status=None,
                 watch_refresh=None, watch_result=None, watch_reconnect=None, watch_validation=None,
                 watch_orders=None, watch_cancel_orders=None, watch_history=None, watch_funds=None,
                 recommendation_report=None, recommendation_overview=None, practice_sales=None, theme_report=None, theme_status=None, theme_budget=None, practice_daily=None, recommendation_request=None, recommendation_status=None, theme_request=None, outcome_request=None, theme_plan=None, watch_delivery=None, actual_daily=None, morning_daily=None, morning_setup=None,daily_mode=None,mock_daily=None,morning_freeze=None,condition_report=None):
        super().__init__()
        self.root, self.token = Path(root), token
        self.user_id, self.chat_id, self.catalog = user_id, chat_id, dict(catalog)
        self.service = None
        self.conditions, self.orders_enabled = conditions, orders_enabled
        self.watch_status = watch_status
        self.watch_refresh, self.watch_result = watch_refresh, watch_result
        self.watch_reconnect = watch_reconnect
        self.watch_validation = watch_validation
        self.watch_history = watch_history
        self.watch_funds = watch_funds
        self.recommendation_report = recommendation_report
        self.recommendation_overview = recommendation_overview
        self.practice_sales = practice_sales
        self.theme_report = theme_report
        self.theme_request = theme_request
        self.outcome_request=outcome_request
        self.theme_status = theme_status
        self.theme_budget = theme_budget
        self.theme_plan=theme_plan
        self.practice_daily = practice_daily
        self.actual_daily=actual_daily
        self.morning_daily=morning_daily
        self.morning_setup=morning_setup
        self.daily_mode=daily_mode
        self.mock_daily=mock_daily
        self.morning_freeze=morning_freeze
        self.condition_report=condition_report
        self.recommendation_request = recommendation_request
        self.recommendation_status = recommendation_status
        self.watch_orders, self.watch_cancel_orders = watch_orders, watch_cancel_orders
        self.watch_delivery=watch_delivery

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
                                                watch_cancel_orders=self.watch_cancel_orders,
                                                watch_history=self.watch_history, watch_funds=self.watch_funds,
                                                recommendation_report=self.recommendation_report,
                                                recommendation_request=self.recommendation_request,
                                                theme_request=self.theme_request,outcome_request=self.outcome_request,theme_plan=self.theme_plan,watch_delivery=self.watch_delivery,actual_daily=self.actual_daily,morning_daily=self.morning_daily,morning_setup=self.morning_setup,daily_mode=self.daily_mode,mock_daily=self.mock_daily,morning_freeze=self.morning_freeze,condition_report=self.condition_report,
                                                recommendation_status=self.recommendation_status,
                                                recommendation_overview=self.recommendation_overview, practice_sales=self.practice_sales, theme_report=self.theme_report, theme_status=self.theme_status, theme_budget=self.theme_budget, practice_daily=self.practice_daily)
            report = self.service.recovery_status(startup=True) if self.orders_enabled else None
            self.status.emit('수신 중 · '+(self.service.safety_text(report) if self.orders_enabled else '조건·조회 관리 · 키움 모의 주문은 PC의 별도 당일 허용 필요'))
            last_banner = self.service.safety_banner() if self.orders_enabled else ''
            next_daily=0
            while not self.isInterruptionRequested():
                receive_once(self.service, transport, self.isInterruptionRequested)
                if not self.isInterruptionRequested():
                    self.service.send_order_notice(transport)
                    if time.monotonic()>=next_daily:
                        self.service.run_morning_schedule(transport)
                        self.service.send_daily_notice(transport)
                        next_daily=time.monotonic()+2
                if self.recommendation_request:
                    self.recommendation_request.send_notice(
                        lambda text: transport.send(self.chat_id, text),
                        active=not self.isInterruptionRequested() and self.service.accepts_search_requests())
                if self.outcome_request:
                    self.outcome_request.send_notice(lambda text:transport.send(self.chat_id,text),
                        active=not self.isInterruptionRequested() and self.service.accepts_search_requests())
                if self.theme_request:
                    self.theme_request.send_notice(transport,self.chat_id,
                        active=not self.isInterruptionRequested() and self.service.accepts_search_requests(),
                        stopped=lambda:self.isInterruptionRequested() or not self.service.accepts_search_requests())
                banner = self.service.safety_banner() if self.orders_enabled else ''
                if banner != last_banner and not self.isInterruptionRequested():
                    self.status.emit('수신 중 · 가짜 서버만 연결\n'+banner)
                    last_banner = banner
                self.msleep(100)
        except ReceiverAlreadyRunning:
            self.status.emit('수신 중지 · 다른 창이나 프로그램에서 주문 연습 수신 중입니다. 먼저 그 창의 수신을 종료하세요.')
        except TelegramConnectionError as error:
            self.status.emit('수신 중지 · '+str(error))
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
