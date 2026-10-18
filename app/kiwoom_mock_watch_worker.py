"""GUI-owned opt-in read-only worker; credentials/account are memory-only."""
import threading
import uuid
from pathlib import Path
from datetime import datetime

from PySide6.QtCore import QThread, Signal
from app.kiwoom_mock_transport import MockTransport
from app.kiwoom_mock_watch import MockWatchSession
from app.runtime_lock import ReceiverLock, ReceiverAlreadyRunning
from app.trading_clock import KST


class MockWatchWorker(QThread):
    status = Signal(str)
    verified = Signal(object)
    notice = Signal(str)

    def __init__(self, root, appkey, secretkey, account, status_store=None, *, allow_recovery=False, clock=None):
        super().__init__()
        self.root = Path(root)
        self._credentials = (appkey,secretkey,account)
        self._start_requested = threading.Event()
        self._stop_requested = threading.Event()
        self.status_store = status_store
        self._verified = False
        self._recovery_credentials = self._credentials if allow_recovery else ()
        self._clock = clock or (lambda: datetime.now(KST))
        self._armed_at = self._clock().astimezone(KST)
        self._ever_connected = False
        self._cleanup_failed = False

    def _status(self, text):
        if self.status_store:
            self.status_store.status(text)
        self.status.emit(text)

    def start_listening(self):
        if self._verified and not self._cancelled():
            self._start_requested.set()

    def stop(self):
        self._stop_requested.set()
        self.requestInterruption()
        self._start_requested.set()

    def _cancelled(self):
        return self._stop_requested.is_set() or self.isInterruptionRequested()

    def _recovery_valid(self):
        now = self._clock().astimezone(KST)
        if now < self._armed_at or now.date() != self._armed_at.date():
            self._recovery_credentials = ()
        return (bool(self._recovery_credentials) and self._ever_connected and not self._cleanup_failed and not self._cancelled()
                and now >= self._armed_at and now.date() == self._armed_at.date())

    def _wait_reconnect(self):
        if not self.status_store or not self._recovery_valid():
            return False
        self.status_store.offer_reconnect()
        self.status.emit('모의 연결이 끊겼어요. 텔레그램 /모의재연결로 다시 연결할 수 있어요.')
        while self._recovery_valid():
            if self.status_store.take_reconnect():
                return self._recovery_valid()
            self._stop_requested.wait(.1)
        return False

    def _serve(self, reconnecting=False):
        session = transport = None
        key = secret = account = None
        try:
            if self._cancelled() or (reconnecting and not self._recovery_valid()):
                return
            if self.status_store:
                self.status_store.reset()
            self._status('모의 계좌와 잔고를 확인하고 있어요.')
            key,secret,account = self._recovery_credentials if reconnecting else self._credentials
            transport = MockTransport(environment='MOCK',profile_id=uuid.uuid4().hex,
                                      cancelled=self._cancelled)
            transport.login(key,secret)
            session = MockWatchSession(transport)
            report = session.verify(account)
            self._credentials = ()
            key = secret = account = None
            if self._cancelled():
                return
            if self.status_store:
                self.status_store.verified(report)
            self._verified = True
            self.verified.emit(report)
            if reconnecting:
                self._status('모의 계좌를 다시 확인했어요. 요청한 모의 알림을 연결하고 있어요.')
            else:
                self._status('모의 계좌 확인 완료 · 2번 알림 받기 시작을 눌러 주세요.')
            # Wait for the separate user action; no automatic socket start.
            while not reconnecting and not self._start_requested.wait(.1):
                if (session._now()-session._verified_at).total_seconds() > 120:
                    raise ValueError('expired')
            if self._cancelled():
                return
            session.start()
            last = None
            while not self._cancelled():
                self._recovery_valid()  # Expire retained recovery information even on a quiet connection.
                if self.status_store and self.status_store.take_refresh():
                    report = session.refresh()
                    if self._cancelled():
                        return
                    self.status_store.finish_refresh(report)
                    self.verified.emit(report)
                for text in session.poll():
                    if self.status_store:
                        self.status_store.notice(text)
                    self.notice.emit(text)
                if self.status_store and session.state == 'RECEIVING':
                    self._ever_connected = True
                    self.status_store.enable_refresh()
                status = session.stream.status
                if status != last:
                    self._status(status)
                    last = status
        finally:
            self._verified = False
            self._credentials = ()
            key = secret = account = None
            try:
                if session is not None:
                    session.close()
                elif transport is not None:
                    transport.close()
            except Exception:
                self._cleanup_failed = True

    def run(self):
        owner, owned = ReceiverLock(self.root/'receiver.lock'), False
        try:
            owner.__enter__()
            owned = True
            reconnecting = False
            while not self._cancelled():
                try:
                    self._serve(reconnecting)
                    break
                except Exception:
                    if not self._wait_reconnect():
                        if not self._cancelled():
                            self._status('연결 확인이 필요해요. 집 PC에서 모의 계좌부터 다시 확인해 주세요.')
                        break
                    reconnecting = True
        except ReceiverAlreadyRunning:
            self._status('다른 창에서 모의 알림을 받고 있어요. 그 창에서 먼저 중지해 주세요.')
        except Exception:
            self._status('모의 연결을 중지했어요. 집 PC에서 다시 확인해 주세요.')
        finally:
            self._credentials = self._recovery_credentials = ()
            if owned:
                owner.__exit__(None,None,None)
            if self.status_store:
                self.status_store.stopped()
            if self._cancelled():
                self._status('모의 알림 받기를 중지했어요. 다시 켜려면 계좌부터 확인해 주세요.')
