"""Explicit local record backup. No credentials, server IO or restoration."""
from contextlib import closing
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import uuid
from app.trading_clock import KST


def _check(db):
    if db.execute('PRAGMA quick_check').fetchall()!=[('ok',)]:
        raise ValueError('기록 파일의 무결성을 확인해야 해요.')
    tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'scans','paper_trades','recommendation_batches'}<=tables:
        raise ValueError('거래·추천 기록 DB가 아니에요.')


def _digest(path):
    result=hashlib.sha256()
    with open(path,'rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''): result.update(block)
    return result.hexdigest()


def backup_records(source,folder,*,mode):
    if mode not in ('DEMO','MOCK','REAL_QUOTES'): raise ValueError('백업 모드를 확인해 주세요.')
    source,folder=Path(source).resolve(),Path(folder).resolve()
    if not source.is_file() or not folder.is_dir(): raise ValueError('기록 파일과 저장할 폴더를 확인해 주세요.')
    target=folder/('krx-records-'+mode+'-'+uuid.uuid4().hex)
    with tempfile.TemporaryDirectory(prefix='.krx-backup-',dir=folder) as temporary:
        temporary=Path(temporary)
        output=temporary/'records.db'
        started=time.monotonic()
        def progress(status,remaining,total):
            if time.monotonic()-started>30: raise TimeoutError('백업 대기 시간이 지났어요.')
        with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as origin:
            _check(origin)
            with closing(sqlite3.connect(output)) as saved:
                origin.backup(saved,pages=256,progress=progress,sleep=.05)
                if saved.execute('PRAGMA journal_mode=DELETE').fetchone()!=('delete',):
                    raise ValueError('단독 기록 파일로 백업을 마치지 못했어요.')
                _check(saved)
        manifest=dict(version=1,mode=mode,created_at=datetime.now(KST).isoformat(timespec='seconds'),
                      size=output.stat().st_size,sha256=_digest(output))
        (temporary/'manifest.json').write_text(json.dumps(manifest,sort_keys=True),encoding='utf-8')
        verify_backup(temporary,expected_mode=mode)
        # A unique new directory is published only after all checks succeed.
        os.rename(temporary,target)
    return target


def verify_backup(folder,*,expected_mode):
    if expected_mode not in ('DEMO','MOCK','REAL_QUOTES'): raise ValueError('백업 모드를 확인해 주세요.')
    folder=Path(folder)
    try:
        # Reject sidecar journals and symlinks: only the hashed standalone DB
        # and its manifest are allowed to influence read-only inspection.
        if folder.is_symlink() or not folder.is_dir(): raise ValueError()
        if {item.name for item in folder.iterdir()} != {'records.db','manifest.json'}: raise ValueError()
        if any((folder/name).is_symlink() or not (folder/name).is_file()
               for name in ('records.db','manifest.json')): raise ValueError()
        with open(folder/'manifest.json','rb') as stream: raw=stream.read(4097)
        manifest=json.loads(raw.decode('utf-8'))
        if (len(raw)>4096 or not isinstance(manifest,dict) or set(manifest)!={'version','mode','created_at','size','sha256'}
                or type(manifest['version']) is not int or manifest['version']!=1 or manifest['mode']!=expected_mode
                or type(manifest['size']) is not int or manifest['size']<=0
                or not isinstance(manifest['sha256'],str) or len(manifest['sha256'])!=64
                or any(c not in '0123456789abcdef' for c in manifest['sha256'])): raise ValueError()
        stamp=datetime.fromisoformat(manifest['created_at'])
        if stamp.tzinfo is None or stamp.utcoffset() is None: raise ValueError()
        path=(folder/'records.db').resolve()
        if path.parent!=folder.resolve(): raise ValueError()
        if path.stat().st_size!=manifest['size'] or _digest(path)!=manifest['sha256']: raise ValueError()
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db: _check(db)
    except (ValueError,TypeError,KeyError,UnicodeError,sqlite3.Error,OSError):
        raise ValueError('백업 파일·모드·무결성을 확인해야 해요. 복원하지 않았어요.') from None
    return dict(mode=expected_mode,created_at=manifest['created_at'],size=manifest['size'])


def inspect_backup(folder,*,expected_mode):
    """Show verified counts only. Never restore, overwrite, or contact a broker."""
    result=verify_backup(folder,expected_mode=expected_mode)
    path=(Path(folder)/'records.db').resolve()
    try:
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
            _check(db)
            result['scan_count']=db.execute('SELECT COUNT(*) FROM scans').fetchone()[0]
            result['trade_count']=db.execute('SELECT COUNT(*) FROM paper_trades').fetchone()[0]
            result['recommendation_count']=db.execute('SELECT COUNT(*) FROM recommendation_batches').fetchone()[0]
    except (ValueError,sqlite3.Error,OSError):
        raise ValueError('백업 내역을 읽을 수 없어요. 복원하지 않았어요.') from None
    return result
