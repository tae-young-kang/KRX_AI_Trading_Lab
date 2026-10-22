import ast
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import pytest
from app.operating_backup import backup,restore,verify
from app.runtime_lock import ReceiverLock,ReceiverAlreadyRunning
from app.storage import Database
from app.krx_history import HistoryStore
from app.config import DEFAULTS
from app.telegram_conditions import ConditionCommands
from test_morning_day import make


def rig(tmp_path):
    root=tmp_path/'data';root.mkdir()
    database=Database(root/'practice_demo.db')
    Database(root/'trading.db')
    HistoryStore(root/'research.db')
    ConditionCommands(root/'telegram_conditions.db',dict(DEFAULTS))
    make(root)
    profile=root/'kiwoom_mock_watch'/'mock_orders'/'profile.id';profile.parent.mkdir(parents=True)
    profile.write_text('a'*32,encoding='ascii')
    (root/'settings.json').write_text('PRIVATE_SETTINGS_NOT_FOR_BACKUP')
    (root/'.env').write_text('PRIVATE_ENV_NOT_FOR_BACKUP')
    (root/'private_export.csv').write_text('PRIVATE_EXPORT_NOT_FOR_BACKUP')
    destination=tmp_path/'backups';destination.mkdir()
    return root,destination,database


def test_full_known_record_bundle_and_fresh_root_restore_exclude_private_settings(tmp_path):
    root,destination,database=rig(tmp_path)
    with database.connect() as db:
        db.execute("INSERT INTO audit(created_at,kind,message) VALUES('2026-10-20','SYNTHETIC','example')")
    bundle=backup(root,destination)
    info=verify(bundle)
    names={entry['name'] for entry in info['files']}
    assert {'trading.db','practice_demo.db','research.db','telegram_conditions.db','morning_virtual/DEMO/2026-10-19.db',
            'kiwoom_mock_watch/mock_orders/profile.id'}<=names
    assert not {'settings.json','.env','private_export.csv'}&names
    assert str(root) not in (bundle/'manifest.json').read_text()
    target=restore(bundle,tmp_path/'restored')
    assert verify(target)==info
    with closing(sqlite3.connect(target/'practice_demo.db')) as db:
        assert db.execute('SELECT message FROM audit').fetchone()[0]=='example'
    assert not (target/'settings.json').exists()
    assert (root/'settings.json').read_text()=='PRIVATE_SETTINGS_NOT_FOR_BACKUP'
    with pytest.raises(ValueError):restore(bundle,target)


@pytest.mark.parametrize('lock',['application.lock','telegram_offline/receiver.lock','kiwoom_mock_watch/receiver.lock'])
def test_running_gui_or_receivers_block_backup_without_publishing(tmp_path,lock):
    root,destination,_=rig(tmp_path)
    with ReceiverLock(root/lock):
        with pytest.raises(ReceiverAlreadyRunning):backup(root,destination)
    assert list(destination.iterdir())==[]


@pytest.mark.parametrize('damage',['hash','sidecar','traversal','wrong_schema'])
def test_invalid_bundle_never_restores_or_overwrites_existing_data(tmp_path,damage):
    root,destination,_=rig(tmp_path)
    bundle=backup(root,destination)
    if damage=='hash':
        with (bundle/'practice_demo.db').open('ab') as stream:stream.write(b'corrupt')
    elif damage=='sidecar':(bundle/'practice_demo.db-wal').write_bytes(b'unexpected')
    elif damage=='traversal':
        manifest=json.loads((bundle/'manifest.json').read_text());manifest['files'][0]['name']='../settings.json'
        (bundle/'manifest.json').write_text(json.dumps(manifest))
    else:
        from app.record_backup import _digest
        path=bundle/'practice_demo.db'
        with closing(sqlite3.connect(path)) as db:db.execute('DROP TABLE scans');db.commit()
        manifest=json.loads((bundle/'manifest.json').read_text())
        entry=next(e for e in manifest['files'] if e['name']=='practice_demo.db')
        entry.update(size=path.stat().st_size,sha256=_digest(path))
        (bundle/'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError):restore(bundle,tmp_path/'restored')
    assert not (tmp_path/'restored').exists()
    assert (root/'practice_demo.db').is_file()


def test_committed_wal_records_are_in_standalone_bundle_without_changing_source(tmp_path):
    root,destination,database=rig(tmp_path)
    with database.connect() as db:
        db.execute("INSERT INTO audit(created_at,kind,message) VALUES('2026-10-20','SYNTHETIC','wal-example')");db.commit()
        assert (root/'practice_demo.db-wal').exists()
        bundle=backup(root,destination)
        assert db.execute('PRAGMA journal_mode').fetchone()[0]=='wal'
    with closing(sqlite3.connect(bundle/'practice_demo.db')) as db:
        assert db.execute('SELECT message FROM audit').fetchone()[0]=='wal-example'
        assert db.execute('PRAGMA journal_mode').fetchone()[0]=='delete'


def test_gui_entrypoint_uses_lock_without_launching_qt_or_mainwindow(tmp_path):
    # Execute only the entry function with synthetic UI objects; no Qt import.
    tree=ast.parse(Path('app/gui.py').read_text(encoding='utf-8'))
    node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='main')
    calls=[]
    class App:
        def setFont(self,font):pass
        def exec(self):calls.append('loop');return 0
    class Window:
        def __init__(self):calls.append('window')
        def show(self):pass
    class Messages:
        @staticmethod
        def information(*args):calls.append('blocked')
    scope=dict(QApplication=lambda args:App(),QFont=lambda *args:None,MainWindow=Window,QMessageBox=Messages,data_dir=lambda:tmp_path)
    exec(compile(ast.Module(body=[node],type_ignores=[]),'entrypoint-only','exec'),scope)
    with ReceiverLock(tmp_path/'application.lock'):scope['main']()
    assert calls==['blocked']
    calls.clear()
    with pytest.raises(SystemExit) as result:scope['main']()
    assert result.value.code==0 and calls==['window','loop']
    with ReceiverLock(tmp_path/'application.lock'):pass
