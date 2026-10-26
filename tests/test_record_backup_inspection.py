"""Offline checks for read-only backup verification. No live clients or order paths."""
import sqlite3

import pytest

from app.record_backup import backup_records, inspect_backup, verify_backup


def source_db(path):
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE scans (id INTEGER)')
        db.execute('CREATE TABLE paper_trades (id INTEGER)')
        db.execute('CREATE TABLE recommendation_batches (id INTEGER)')
        db.execute('INSERT INTO scans VALUES (1)')
        db.execute('INSERT INTO paper_trades VALUES (1)')
        db.execute('INSERT INTO recommendation_batches VALUES (1)')


def test_inspect_reads_counts_no_writes(tmp_path):
    source = tmp_path / 'source.db'
    source_db(source)
    backup = backup_records(source, tmp_path, mode='DEMO')
    before = (backup / 'records.db').read_bytes()
    summary = inspect_backup(backup, expected_mode='DEMO')
    assert [summary[k] for k in ('scan_count', 'trade_count', 'recommendation_count')] == [1, 1, 1]
    assert (backup / 'records.db').read_bytes() == before
    assert {p.name for p in backup.iterdir()} == {'records.db', 'manifest.json'}


def test_wrong_mode_invalid_hash_extra_sqlite_files(tmp_path):
    source = tmp_path / 'source.db'
    source_db(source)
    backup = backup_records(source, tmp_path, mode='MOCK')
    for bad_mode in ('DEMO', 'REAL_QUOTES'):
        with pytest.raises(ValueError):
            inspect_backup(backup, expected_mode=bad_mode)
    (backup / 'records.db-wal').write_bytes(b'junk')
    with pytest.raises(ValueError):
        verify_backup(backup, expected_mode='MOCK')
    (backup / 'records.db-wal').unlink()
    with open(backup / 'records.db', 'ab') as stream:
        stream.write(b'modified')
    with pytest.raises(ValueError):
        inspect_backup(backup, expected_mode='MOCK')


def test_symlink_manifest_rejected(tmp_path):
    source = tmp_path / 'source.db'
    source_db(source)
    backup = backup_records(source, tmp_path, mode='REAL_QUOTES')
    manifest = backup / 'manifest.json'
    other = tmp_path / 'copy.json'
    other.write_bytes(manifest.read_bytes())
    manifest.unlink()
    try:
        manifest.symlink_to(other)
    except (NotImplementedError, OSError):
        pytest.skip('Symlink creation is not available in this environment')
    with pytest.raises(ValueError):
        verify_backup(backup, expected_mode='REAL_QUOTES')
