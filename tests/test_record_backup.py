import json
import sqlite3
from app.storage import Database
from app.record_backup import backup_records,verify_backup
import pytest


def test_consistent_backup_verifies_without_paths_or_settings(tmp_path):
    db=Database(tmp_path/'practice.db')
    with db.connect() as conn:
        conn.execute("INSERT INTO audit(created_at,kind,message) VALUES('2026-10-19','TEST','synthetic')")
    (tmp_path/'settings.json').write_text('PRIVATE_CONFIGURATION')
    folder=backup_records(db.path,tmp_path,mode='DEMO')
    assert verify_backup(folder,expected_mode='DEMO')['mode']=='DEMO'
    assert {path.name for path in folder.iterdir()}=={'records.db','manifest.json'}
    manifest=json.loads((folder/'manifest.json').read_text())
    assert 'source' not in manifest and str(tmp_path) not in str(manifest)
    with sqlite3.connect(folder/'records.db') as conn:
        assert conn.execute('SELECT COUNT(*) FROM audit').fetchone()[0]==1
    second=backup_records(db.path,tmp_path,mode='DEMO')
    assert second!=folder and folder.exists()
    with pytest.raises(ValueError): verify_backup(folder,expected_mode='MOCK')


def test_changed_backup_or_invalid_source_is_not_accepted(tmp_path):
    db=Database(tmp_path/'practice.db')
    folder=backup_records(db.path,tmp_path,mode='MOCK')
    with open(folder/'records.db','ab') as stream: stream.write(b'changed')
    with pytest.raises(ValueError): verify_backup(folder,expected_mode='MOCK')
    invalid=tmp_path/'unrelated.db'
    with sqlite3.connect(invalid) as conn: conn.execute('CREATE TABLE unrelated(value INTEGER)')
    before={path.name for path in tmp_path.iterdir()}
    with pytest.raises(ValueError): backup_records(invalid,tmp_path,mode='MOCK')
    assert {path.name for path in tmp_path.iterdir()}==before


def test_open_wal_source_is_copied_without_changing_source_mode(tmp_path):
    db=Database(tmp_path/'practice.db')
    with db.connect() as source:
        source.execute("INSERT INTO audit(created_at,kind,message) VALUES('2026-10-19','TEST','in-wal')")
        source.commit()
        assert (tmp_path/'practice.db-wal').exists()
        folder=backup_records(db.path,tmp_path,mode='REAL_QUOTES')
        assert source.execute('PRAGMA journal_mode').fetchone()[0]=='wal'
    with sqlite3.connect(folder/'records.db') as saved:
        assert saved.execute('SELECT message FROM audit').fetchone()[0]=='in-wal'
        assert saved.execute('PRAGMA journal_mode').fetchone()[0]=='delete'
