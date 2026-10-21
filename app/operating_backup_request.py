"""Explicit local maintenance requests, executed only after the GUI closes."""
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from app import operating_backup
from app.runtime_lock import ReceiverLock


def _plain(path):
    path=Path(path).absolute()
    if any(part.is_symlink() or (hasattr(part,'is_junction') and part.is_junction())
           for part in (path,*path.parents)):
        raise ValueError('링크가 아닌 일반 폴더를 선택해 주세요.')
    return path.resolve()


def _outside_bundle(path):
    # Never publish new files inside an existing manifest-owned snapshot.
    # A manifest of any kind is a conservative boundary; do not read/repair it.
    if any((part/'manifest.json').exists() or (part/'manifest.json').is_symlink()
           for part in (path,*path.parents)):
        raise ValueError('기존 백업 폴더 밖의 저장 위치를 선택해 주세요.')


@dataclass(frozen=True)
class OperatingRequest:
    action: str
    root: Path
    folder: Path
    target: Path|None=None

    def validate(self):
        root,folder=_plain(self.root),_plain(self.folder)
        if self.action not in ('backup','restore') or not root.is_dir() or not folder.is_dir():
            raise ValueError('작업 종류와 폴더를 확인해 주세요.')
        if self.action=='backup':
            if self.target is not None or folder.is_relative_to(root):
                raise ValueError('현재 기록 폴더 밖의 백업 위치를 선택해 주세요.')
            _outside_bundle(folder)
        else:
            if self.target is None:raise ValueError('새 복구 폴더가 필요해요.')
            target=_plain(self.target)
            if (target.exists() or not target.parent.is_dir() or target.is_relative_to(root)
                    or target.is_relative_to(folder)):
                raise ValueError('현재 기록과 백업 밖의 새 복구 폴더를 선택해 주세요.')
            _outside_bundle(target)
        return self


def execute(request):
    """No app start, data-root switch, permission restore, or order dispatch."""
    try:
        if type(request) is not OperatingRequest:raise ValueError()
        request.validate()
        if request.action=='backup':
            output=operating_backup.backup(request.root,request.folder)
        else:
            # Restore itself only copies a bundle. Also prevent another active
            # instance from overlapping this explicitly deferred maintenance.
            with ExitStack() as locks:
                for name in ('application.lock','telegram_offline/receiver.lock','kiwoom_mock_watch/receiver.lock'):
                    locks.enter_context(ReceiverLock(Path(request.root)/name))
                output=operating_backup.restore(request.folder,request.target)
        return dict(ok=True,action=request.action,path=str(output))
    except Exception:
        return dict(ok=False,message='작업을 마치지 못했어요. 프로그램·수신 종료와 폴더·백업 무결성을 확인해 주세요. 현재 기록을 덮거나 주문을 실행하지 않았어요.')
