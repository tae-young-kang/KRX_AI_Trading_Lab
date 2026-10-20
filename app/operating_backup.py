"""Explicit offline operational-record bundles. Never upload or enable orders."""
import argparse
from contextlib import ExitStack,closing
from datetime import date,datetime
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import time
import uuid
from app.record_backup import _digest
from app.runtime_lock import ReceiverLock
from app.trading_clock import KST


REQUIRED={
    'trading.db':{'paper_trades'},  # Legacy mixed-context records stay separate.
    **{f'practice_{m}.db':{'scans','paper_trades','recommendation_batches'} for m in ('demo','mock','real_quotes')},
    'research.db':{'daily_bars'},'telegram_conditions.db':{'condition_current','condition_requests'},
    'actual_fill_records.db':{'actual_meta','actual_fills'},
    'telegram_offline/fake_broker.db':{'meta','orders','positions'},
    'telegram_offline/fake_orders.db':{'engine_meta','orders','actions'},
    'telegram_offline/telegram_commands.db':{'tg_sessions','tg_updates'},
    'kiwoom_mock_watch/observations.db':{'mock_observations'},
    'kiwoom_mock_watch/mock_orders/telegram_intents.db':{'intents','observations'},
    'kiwoom_mock_watch/mock_orders/dispatch.db':{'mock_identity','mock_dispatch'},
    'kiwoom_mock_watch/mock_orders/fills.db':{'fill_meta','fill_events'},
    'kiwoom_mock_watch/mock_orders/protection.db':{'protection_meta','protection_plans'},
}
PROFILE='kiwoom_mock_watch/mock_orders/profile.id'


def allowed(name):
    if name in REQUIRED or name==PROFILE:return True
    match=re.fullmatch(r'morning_virtual/(DEMO|MOCK|REAL_QUOTES)/([0-9]{4}-[0-9]{2}-[0-9]{2})\.db',name)
    if not match:return False
    try:return date.fromisoformat(match[2]).isoformat()==match[2]
    except ValueError:return False


def checked_path(root,name):
    if not isinstance(name,str) or not allowed(name):raise ValueError('운영 백업의 파일 범위를 확인해 주세요.')
    path=root.joinpath(*name.split('/'))
    if any(part.is_symlink() for part in [path,*path.parents] if part!=root.parent):
        raise ValueError('백업 파일의 링크 경로를 사용할 수 없어요.')
    if not path.resolve().is_relative_to(root.resolve()):raise ValueError('백업 파일의 저장 위치를 확인해 주세요.')
    return path


def check_file(path,name):
    if name==PROFILE:
        if path.stat().st_size!=32 or not re.fullmatch('[a-f0-9]{32}',path.read_text(encoding='ascii')):
            raise ValueError('모의 기록 식별자를 확인해 주세요.')
        return
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.execute('PRAGMA trusted_schema=OFF')
        if db.execute('PRAGMA quick_check').fetchall()!=[('ok',)]:raise ValueError('운영 기록 무결성 확인 실패')
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        expected=REQUIRED.get(name,{'morning_cohort','morning_marks','paper_trades','paper_exits'})
        if not expected<=tables:raise ValueError('운영 기록의 종류가 저장 위치와 달라요.')


def verify(folder):
    folder=Path(folder)
    if folder.is_symlink() or not folder.is_dir() or (folder/'manifest.json').is_symlink():
        raise ValueError('백업 폴더·목록의 링크 경로를 사용할 수 없어요.')
    folder=folder.resolve()
    raw=(folder/'manifest.json').read_bytes()
    if len(raw)>2_000_000:raise ValueError('백업 목록이 너무 커요.')
    info=json.loads(raw)
    if (not isinstance(info,dict) or set(info)!={'version','created_at','files'} or type(info['version']) is not int
            or info['version']!=1 or not isinstance(info['files'],list) or not 1<=len(info['files'])<=10000):
        raise ValueError('운영 백업 목록을 확인해 주세요.')
    stamp=datetime.fromisoformat(info['created_at'])
    if stamp.tzinfo is None or stamp.utcoffset() is None:raise ValueError('백업 시각을 확인해 주세요.')
    names=set()
    for entry in info['files']:
        if not isinstance(entry,dict) or set(entry)!={'name','size','sha256'}:raise ValueError('백업 파일 정보를 확인해 주세요.')
        name=entry['name'];path=checked_path(folder,name)
        if (name in names or type(entry['size']) is not int or entry['size']<=0
                or not isinstance(entry['sha256'],str) or not re.fullmatch('[a-f0-9]{64}',entry['sha256'])
                or not path.is_file() or path.stat().st_size!=entry['size'] or _digest(path)!=entry['sha256']):
            raise ValueError('운영 백업 파일의 크기·해시를 확인해 주세요.')
        names.add(name);check_file(path,name)
    actual=set()
    for path in folder.rglob('*'):
        if path.is_symlink() or not path.resolve().is_relative_to(folder):raise ValueError('백업 링크 경로는 사용할 수 없어요.')
        if path.is_file():actual.add(path.relative_to(folder).as_posix())
    if actual!=names|{'manifest.json'}:raise ValueError('백업에 목록 외 파일이나 DB 저널이 있어요.')
    return info


def backup(root,folder):
    root,folder=Path(root).resolve(),Path(folder).resolve()
    if not root.is_dir() or not folder.is_dir() or folder.is_relative_to(root):raise ValueError('기록 폴더 밖의 기존 백업 폴더를 선택해 주세요.')
    target=folder/('krx-operations-'+uuid.uuid4().hex)
    with ExitStack() as locks:
        for name in ('application.lock','telegram_offline/receiver.lock','kiwoom_mock_watch/receiver.lock'):
            locks.enter_context(ReceiverLock(root/name))
        names=[name for name in [*REQUIRED,PROFILE] if checked_path(root,name).is_file()]
        for mode in ('DEMO','MOCK','REAL_QUOTES'):
            for path in (root/'morning_virtual'/mode).glob('*.db'):
                name=path.relative_to(root).as_posix()
                if allowed(name):names.append(name)
        if not names:raise ValueError('백업할 운영 기록이 없어요.')
        with tempfile.TemporaryDirectory(prefix='.krx-operations-',dir=folder) as temporary:
            staged=Path(temporary);entries=[]
            for name in sorted(names):
                source=checked_path(root,name);check_file(source,name)
                output=staged.joinpath(*name.split('/'));output.parent.mkdir(parents=True,exist_ok=True)
                if name==PROFILE:shutil.copyfile(source,output)
                else:
                    started=time.monotonic()
                    def progress(status,remaining,total):
                        if time.monotonic()-started>30:raise TimeoutError('기록 백업 대기 시간이 지났어요.')
                    with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as origin,closing(sqlite3.connect(output)) as saved:
                        origin.backup(saved,pages=256,progress=progress,sleep=.05)
                        if saved.execute('PRAGMA journal_mode=DELETE').fetchone()!=('delete',):raise ValueError('DB 저널 정리 실패')
                entries.append(dict(name=name,size=output.stat().st_size,sha256=_digest(output)))
            info=dict(version=1,created_at=datetime.now(KST).isoformat(),files=entries)
            (staged/'manifest.json').write_text(json.dumps(info,ensure_ascii=False,sort_keys=True),encoding='utf-8')
            verify(staged)
            os.rename(staged,target)
    return target


def restore(folder,target):
    folder=Path(folder).resolve();target=Path(target).absolute()
    if target.exists() or target.is_symlink() or not target.parent.is_dir() or target.resolve().is_relative_to(folder):
        raise ValueError('복원은 백업 밖의 새 폴더에만 할 수 있어요. 기존 기록을 덮지 않았어요.')
    info=verify(folder)
    with tempfile.TemporaryDirectory(prefix='.krx-restore-',dir=target.parent) as temporary:
        staged=Path(temporary)
        for entry in info['files']:
            output=staged.joinpath(*entry['name'].split('/'));output.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(checked_path(folder,entry['name']),output)
        (staged/'manifest.json').write_text(json.dumps(info,ensure_ascii=False,sort_keys=True),encoding='utf-8')
        verify(staged)
        if target.exists():raise ValueError('복원 폴더가 이미 생겼어요. 덮지 않았어요.')
        os.rename(staged,target)
    return target


def main():
    parser=argparse.ArgumentParser(description='프로그램 종료 후 운영 기록 백업/새 폴더 복원. 키·설정 제외, 주문/통신 없음.')
    parser.add_argument('action',choices=('backup','verify','restore'))
    parser.add_argument('folder')
    parser.add_argument('target',nargs='?')
    args=parser.parse_args()
    try:
        if args.action=='backup':
            if args.target:raise ValueError()
            from app.config import data_dir
            result=backup(data_dir(),args.folder)
            print('운영 기록 백업 완료:',result)
        elif args.action=='verify':
            if args.target:raise ValueError()
            print('운영 기록 검증 완료 · 파일',len(verify(args.folder)['files']))
        else:
            if not args.target:raise ValueError()
            print('새 폴더 복원 완료:',restore(args.folder,args.target))
            print('연결 키·설정은 복원하지 않았어요. 실행 전 모드/기록과 미확인 주문을 먼저 확인하세요.')
        return 0
    except Exception:
        print('작업을 마치지 못했어요. 프로그램/수신 종료와 폴더·파일 무결성을 확인해 주세요. 기존 기록은 덮지 않았어요.')
        return 1


if __name__=='__main__':raise SystemExit(main())
