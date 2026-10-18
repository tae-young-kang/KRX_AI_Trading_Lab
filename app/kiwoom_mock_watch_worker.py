"""GUI-owned opt-in read-only worker; credentials/account are memory-only."""
import threading
import uuid
from pathlib import Path

from PySide6.QtCore import QThread, Signal
from app.kiwoom_mock_transport import MockTransport
from app.kiwoom_mock_watch import MockWatchSession
from app.runtime_lock import ReceiverLock, ReceiverAlreadyRunning


class MockWatchWorker(QThread):
    status = Signal(str)
    verified = Signal(object)
    notice = Signal(str)

    def __init__(self, root, appkey, secretkey, account, status_store=None):
        super().__init__()
        self.root = Path(root)
        self._credentials = (appkey,secretkey,account)
        self._start_requested = threading.Event()
        self._stop_requested = threading.Event()
        self.status_store = status_store
        self._verified = False

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

    def run(self):
        session = transport = None
        owner, owned = ReceiverLock(self.root/'receiver.lock'), False
        key = secret = account = None
        try:
            owner.__enter__()
            owned = True
            if self._cancelled():
                return
            if self.status_store:
                self.status_store.reset()
            self._status('모의 계좌와 잔고를 확인하고 있어요.')
            key,secret,account = self._credentials
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
            self._status('모의 계좌 확인 완료 · 2번 알림 받기 시작을 눌러 주세요.')
            # Wait for the separate user action; no automatic socket start.
            while not self._start_requested.wait(.1):
                if (session._now()-session._verified_at).total_seconds() > 120:
                    raise ValueError('expired')
            if self._cancelled():
                return
            session.start()
            last = None
            while not self._cancelled():
                for text in session.poll():
                    if self.status_store:
                        self.status_store.notice(text)
                    self.notice.emit(text)
                status = session.stream.status
                if status != last:
                    self._status(status)
                    last = status
        except ReceiverAlreadyRunning:
            self._status('다른 창에서 모의 알림을 받고 있어요. 그 창에서 먼저 중지해 주세요.')
        except Exception:
            if not self._cancelled():
                self._status('연결 확인이 필요해요. 1번으로 모의 계좌부터 다시 확인해 주세요.')
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
                pass
            if owned:
                owner.__exit__(None,None,None)
            if self.status_store:
                self.status_store.stopped()
            if self._cancelled():
                self._status('모의 알림 받기를 중지했어요. 다시 켜려면 계좌부터 확인해 주세요.')
