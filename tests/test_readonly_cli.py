"""Retrieval must work without migrations or invocation writes (P77)."""
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import db
import memory_store as memory
import recall_memory as cli


@pytest.fixture
def evidence(tmp_path):
    path = tmp_path / 'existing ? # store.db'
    transcript = tmp_path / 'trace.jsonl'
    transcript.write_text(json.dumps({'type': 'user', 'uuid': 'one',
        'message': {'role': 'user', 'content': 'Keep the amber sparrow decision.'}}) + '\n')
    c = db.get_connection(path)
    memory.index_file(c, transcript, agent='claude', session_id='read-test', cwd=str(tmp_path))
    c.commit()
    block = c.execute('SELECT id FROM memory_blocks').fetchone()[0]
    c.execute('PRAGMA journal_mode=DELETE')
    c.close()
    return path, block


@pytest.mark.parametrize('command', ['search', 'get', 'brief', 'status', 'sources', 'export', 'backup'])
def test_reads_do_not_write_or_migrate(evidence, tmp_path, monkeypatch, capsys, command):
    path, block = evidence
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    def forbidden(*a, **kw):
        pytest.fail('Retrieval opened a writer or ran a migration')
    monkeypatch.setattr(cli, 'get_connection', forbidden)
    monkeypatch.setattr(db, '_apply_migrations', forbidden)
    args = {'search': ['amber', '--all'], 'get': [block],
            'brief': ['--all'], 'status': [], 'sources': [],
            'export': ['claude:read-test'], 'backup': [str(tmp_path / 'backup.db')]}[command]
    assert cli.main(['--db', str(path), command, *args]) == 0
    output = json.loads(capsys.readouterr().out)
    if command == 'search':
        assert output['hits'][0]['block_id'] == block
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert not Path(str(path) + '-wal').exists()


def test_missing_database_is_not_created(tmp_path, capsys):
    path = tmp_path / 'missing' / 'memory.db'
    assert cli.main(['--db', str(path), 'status']) == 1
    assert not path.parent.exists()
    assert 'error' in json.loads(capsys.readouterr().err)


def test_old_schema_is_not_silently_migrated(evidence, capsys):
    path, _ = evidence
    c = sqlite3.connect(path)
    c.execute('PRAGMA user_version=8')
    c.close()
    before = path.read_bytes()
    assert cli.main(['--db', str(path), 'status']) == 1
    assert 'schema' in json.loads(capsys.readouterr().err)['error']
    assert path.read_bytes() == before


def test_new_read_sees_committed_wal(evidence):
    path, _ = evidence
    writer = sqlite3.connect(path)
    writer.execute('PRAGMA journal_mode=WAL')
    # Prepare WAL sidecars through the writer. Apple's SQLite cannot create
    # missing sidecars from mode=ro even in a writable directory.
    writer.execute('BEGIN IMMEDIATE')
    writer.rollback()
    first = db.get_read_connection(path)
    first.close()
    writer.execute("UPDATE memory_blocks SET text='Fresh committed WAL evidence'")
    writer.commit()
    second = db.get_read_connection(path)
    assert second.execute('SELECT text FROM memory_blocks').fetchone()[0] == 'Fresh committed WAL evidence'
    with pytest.raises(sqlite3.OperationalError):
        second.execute("UPDATE memory_blocks SET text='forbidden'")
    second.close()
    writer.close()


def test_access_failure_is_actionable_without_traceback(monkeypatch, capsys):
    def denied(*a):
        error = sqlite3.OperationalError('unable to open database file')
        error.sqlite_errorcode = 14  # SQLITE_CANTOPEN, including Python 3.9
        raise error
    monkeypatch.setattr(cli, 'get_read_connection', denied)
    assert cli.main(['search', 'amber']) == 1
    streams = capsys.readouterr()
    error = json.loads(streams.err)
    assert error['code'] == 'store_access'
    assert 'same read and scope' in error['next_action']
    assert not streams.out


@pytest.mark.parametrize('message, expected_code', [
    ('unable to open database file', 'store_access'),
    ('attempt to write a readonly database', 'store_access'),
    ('access permission denied', 'store_access'),
    ('database is locked', None),
    ('no such table: memory_blocks', None),
])
def test_python39_sqlite_errors_have_safe_classification(monkeypatch, capsys, message, expected_code):
    for name in ('SQLITE_CANTOPEN', 'SQLITE_READONLY', 'SQLITE_PERM'):
        monkeypatch.delattr(sqlite3, name, raising=False)
    def denied(*args):
        raise sqlite3.OperationalError(message)  # No sqlite_errorcode before 3.11.
    monkeypatch.setattr(cli, 'get_read_connection', denied)
    assert cli.main(['search', 'amber']) == 1
    output = json.loads(capsys.readouterr().err)
    assert output['error'] == message
    assert output.get('code') == expected_code
