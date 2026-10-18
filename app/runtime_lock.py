"""OS-owned lock: process death releases ownership; never delete the lock file."""
import os
from pathlib import Path


class ReceiverAlreadyRunning(RuntimeError):
    pass


class ReceiverLock:
    def __init__(self, path):
        self.path = Path(path)
        self.file = None

    def __enter__(self):
        if self.file is not None:
            raise ReceiverAlreadyRunning('이미 수신 소유권을 가지고 있습니다.')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open('a+b')
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise ReceiverAlreadyRunning('다른 창이나 프로그램에서 주문 연습을 수신 중입니다.') from None
        self.file = handle
        return self

    def __exit__(self, *args):
        if self.file is not None:
            try:
                self.file.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            finally:
                self.file.close()
                self.file = None
