"""Dedicated quote-only receiver: never shares a mock/account order stream."""
from PySide6.QtCore import QThread,Signal
from app.quote_capture import Capture


class QuoteCaptureWorker(QThread):
    completed=Signal(str)

    def __init__(self,root,auth_factory,*,active):
        super().__init__();self.root=root;self.auth_factory=auth_factory;self.active=active

    def run(self):
        state=Capture(self.root,self.auth_factory).run(active=lambda:not self.isInterruptionRequested() and self.active())
        self.completed.emit(state)

    def stop(self):self.requestInterruption()
