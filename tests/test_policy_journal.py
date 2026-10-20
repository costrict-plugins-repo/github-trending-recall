"""External suppression survives content rollback and interrupted owner changes."""
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
from db import get_connection
import memory_store as memory
import recall_privacy as policy
from recall_memory import parser, run
from test_capture_policy import write


@pytest.fixture
def store(tmp_path):
    conn = get_connection(tmp_path / 'store.db')
    yield conn, tmp_path
    conn.close()


def test_raw_old_content_restore_replays_external_suppression(store):
    conn, root = store
    old = sqlite3.connect(root / 'old.db')
    conn.backup(old)
    policy.set_mode(conn, 'codex:s', 'off')
    old.backup(conn)  # No capture-policy rows/required marker in the old content.
    old.close()
    assert not conn.execute('SELECT * FROM recall_capture_policy').fetchall()
    # An already-open updated writer checks the journal before retaining content.
    assert memory.index_file(conn, write(root / 's.jsonl'), agent='codex')['state'] == 'capture_disabled'
    conn.rollback()
    reopened = get_connection(root / 'store.db')
    assert reopened.execute('SELECT mode FROM recall_capture_policy').fetchone()[0] == 'off'
    reopened.close()


def test_off_crash_after_external_commit_before_db_commit_stays_off(store, monkeypatch):
    conn, root = store
    original = policy.write_journal
    def crash(*args):
        original(*args)
        raise RuntimeError('injected crash')
    monkeypatch.setattr(policy, 'write_journal', crash)
    with pytest.raises(RuntimeError):
        policy.set_mode(conn, 'codex:s', 'off')
    conn.rollback()
    assert not conn.execute('SELECT * FROM recall_capture_policy').fetchall()
    assert policy.mode(conn, 'codex:s') == 'off'
    later = get_connection(root / 'store.db')
    assert policy.mode(later, 'codex:s') == 'off'
    later.close()


def test_grant_crash_after_db_commit_before_journal_change_stays_off(store, monkeypatch):
    conn, root = store
    policy.set_mode(conn, 'codex:s', 'off')
    def crash(*args):
        raise RuntimeError('injected crash')
    monkeypatch.setattr(policy, 'write_journal', crash)
    with pytest.raises(RuntimeError):
        policy.set_mode(conn, 'codex:s', 'shared', allow_backfill=True)
    assert conn.execute('SELECT mode FROM recall_capture_policy').fetchone()[0] == 'shared'
    assert policy.mode(conn, 'codex:s') == 'off'
    later = get_connection(root / 'store.db')
    assert policy.mode(later, 'codex:s') == 'off'
    later.close()


@pytest.mark.parametrize('damage', ['missing', 'json', 'version', 'permission', 'oversize', 'symlink', 'hardlink'])
def test_damaged_or_missing_required_journal_fails_closed(store, damage):
    conn, root = store
    policy.set_mode(conn, 'codex:s', 'off')
    path = policy.journal_path(conn)
    if damage == 'missing':
        path.unlink()
    elif damage == 'json':
        path.write_text('{')
    elif damage == 'version':
        path.write_text(json.dumps({'version': 2, 'off': []}))
    elif damage == 'permission':
        path.chmod(0o644)
    elif damage == 'oversize':
        path.write_bytes(b' ' * (1024 * 1024 + 1))
    elif damage == 'symlink':
        other = root / 'external.json'
        path.rename(other)
        path.symlink_to(other)
    else:
        os.link(path, root / 'linked.json')
    with pytest.raises((ValueError, OSError)):
        get_connection(root / 'store.db')
    with pytest.raises((ValueError, OSError)):
        memory.index_file(conn, write(root / 's.jsonl'), agent='codex')
    assert conn.execute('SELECT count(*) FROM memory_blocks').fetchone()[0] == 0


def test_explicit_restore_of_portable_off_backup_creates_target_journal(store):
    conn, root = store
    source = write(root / 's.jsonl')
    memory.index_file(conn, source, agent='codex')
    conn.commit()
    policy.set_mode(conn, 'codex:s', 'off')
    backup = root / 'backup.db'
    run(parser().parse_args(['backup', str(backup)]), conn)
    # An arbitrary writable copy cannot silently lose its required journal.
    with pytest.raises(ValueError, match='journal is missing'):
        get_connection(backup)
    target = get_connection(root / 'target.db')
    run(parser().parse_args(['restore', str(backup), '--yes']), target)
    assert policy.mode(target, 'codex:s') == 'off'
    assert policy.journal_path(target).exists()
    assert memory.search(target, 'amber')  # Off preserves existing evidence.
    target.close()


def test_successful_grant_is_explicit_and_missing_empty_ledger_still_fails(store):
    conn, root = store
    policy.set_mode(conn, 'codex:s', 'off')
    policy.set_mode(conn, 'codex:s', 'shared', allow_backfill=True)
    assert policy.mode(conn, 'codex:s') == 'shared'
    assert json.loads(policy.journal_path(conn).read_text())['off'] == []
    policy.journal_path(conn).unlink()
    with pytest.raises(ValueError, match='journal is missing'):
        policy.mode(conn, 'codex:s')


def test_control_lock_refuses_simultaneous_policy_and_restore(store):
    conn, root = store
    backup = root / 'old.db'
    run(parser().parse_args(['backup', str(backup)]), conn)
    other = get_connection(root / 'store.db')
    with policy.journal_lock(conn):
        with pytest.raises(ValueError, match='operation is active'):
            policy.set_mode(other, 'codex:s', 'off')
        with pytest.raises(ValueError, match='operation is active'):
            run(parser().parse_args(['restore', str(backup), '--yes']), other)
    assert not other.execute('SELECT * FROM recall_capture_policy').fetchall()
    other.close()


def test_journal_contains_only_version_and_suppressed_identities(store):
    conn, _ = store
    policy.set_mode(conn, 'codex:s', 'off')
    path = policy.journal_path(conn)
    assert json.loads(path.read_text()) == {'version': 1, 'off': ['codex:s']}
    assert path.stat().st_mode & 0o777 == 0o600
    assert not list(path.parent.glob('.' + path.name + '.*'))


def test_duplicate_json_fields_are_not_interpreted_as_a_grant(store):
    conn, _ = store
    policy.set_mode(conn, 'codex:s', 'off')
    policy.journal_path(conn).write_text('{"version":1,"off":["codex:s"],"off":[]}')
    with pytest.raises(ValueError, match='Duplicate'):
        policy.mode(conn, 'codex:s')


def test_legacy_prune_never_deletes_another_agents_same_bare_id(store):
    from db import prune_session
    from prompt_submit import index_transcript
    conn, root = store
    index_transcript(conn, 's', str(write(root / 'claude.jsonl', 'claude')))
    memory.index_file(conn, write(root / 'codex.jsonl'), agent='codex')
    conn.commit()
    prune_session(conn, 's')
    assert [r[0] for r in conn.execute('SELECT source_key FROM memory_sources')] == ['codex:s']
    assert memory.search(conn, 'amber')
    assert not conn.execute('SELECT * FROM sessions').fetchall()
    assert not memory.verify_schema(conn)


def test_recovery_does_not_overwrite_a_concurrent_acknowledged_grant(store, monkeypatch):
    conn, root = store
    policy.set_mode(conn, 'codex:s', 'off')
    other = get_connection(root / 'store.db')
    conn.execute('DELETE FROM recall_capture_policy')
    conn.commit()  # Emulate old content that needs journal replay.
    original = policy.journal_state
    intervened = False
    def read_then_grant(c):
        nonlocal intervened
        entries = original(c)
        if not intervened:
            intervened = True
            policy.set_mode(other, 'codex:s', 'shared', allow_backfill=True)
        return entries
    monkeypatch.setattr(policy, 'journal_state', read_then_grant)
    policy.recover_journal(conn)
    assert policy.mode(conn, 'codex:s') == 'shared'
    assert conn.execute('SELECT mode FROM recall_capture_policy').fetchone()[0] == 'shared'
    other.close()
