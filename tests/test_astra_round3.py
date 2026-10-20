"""Astra round-3 boundary tests (review of 2a4c21b..54bea2e, 2026-09-05), adopted as the
regression suite for R3-01..R3-05 plus their passing controls. Authored by Astra; the only
change is the repo-relative root. Temporary stores only (conftest isolates HOME)."""
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'hooks')]
import memory_store as memory
from db import get_connection, SCHEMA_VERSION
from recall_memory import parser, run


def record(text, key, role='assistant'):
    return {'type': role, 'uuid': key, 'sessionId': 'round3',
            'timestamp': '2026-09-05T12:00:00Z',
            'message': {'role': role, 'content': [{'type': 'text', 'text': text}]}}


def trace(path, records):
    path.write_text(''.join(json.dumps(r) + '\n' for r in records))


@pytest.fixture
def store(tmp_path):
    conn = get_connection(tmp_path / 'target.db')
    path = tmp_path / 'trace.jsonl'
    trace(path, [record('irreplaceable sentinel evidence', 'a', 'user'),
                 record('second passage evidence', 'b')])
    memory.index_file(conn, path, session_id='round3', cwd=str(tmp_path))
    conn.commit()
    yield conn, tmp_path, path
    conn.close()


def test_restore_refuses_incomplete_current_schema_before_replacing_target(store):
    conn, tmp, _ = store
    backup = tmp / 'incomplete.db'
    other = get_connection(backup)
    other.execute('DROP TABLE memory_fts')
    other.commit()
    assert other.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    other.close()
    try:
        run(parser().parse_args(['restore', str(backup), '--yes']), conn)
    except (ValueError, sqlite3.DatabaseError):
        pass
    # Rejection must leave the user's data AND retrieval intact.
    assert memory.search(conn, 'irreplaceable')


@pytest.mark.parametrize('filename', ['backup?#copy.db', 'backup with space.db'])
def test_restore_supports_literal_backup_filenames(store, filename):
    conn, tmp, _ = store
    backup = tmp / filename
    run(parser().parse_args(['backup', str(backup)]), conn)
    result = run(parser().parse_args(['restore', str(backup), '--yes']), conn)
    assert result['integrity_check'] == 'ok'
    assert memory.search(conn, 'irreplaceable')


def seed_v8_vectors(conn, values):
    ids = [r[0] for r in conn.execute('SELECT id FROM memory_chunks ORDER BY id')]
    conn.execute('CREATE TABLE memory_semantic_config(id INTEGER PRIMARY KEY CHECK(id=1), path TEXT, model TEXT)')
    conn.execute("INSERT INTO memory_semantic_config VALUES(1,'/unused/offline/model','model-fingerprint')")
    conn.executemany('INSERT INTO memory_vectors(chunk_id,model,vector) VALUES(?,?,?)',
                     [(i, 'model-fingerprint', json.dumps(v)) for i, v in zip(ids, values)])
    conn.execute('PRAGMA user_version=8')
    conn.commit()


def test_vector_migration_drops_float32_overflow_without_wedging_store(store):
    conn, tmp, _ = store
    seed_v8_vectors(conn, [[1e100, 0.0], [0.1, 0.2]])
    conn.close()
    migrated = get_connection(tmp / 'target.db')
    try:
        assert migrated.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION
        assert migrated.execute('SELECT count(*) FROM memory_vectors').fetchone()[0] == 1
        assert memory.search(migrated, 'irreplaceable')
    finally:
        migrated.close()


def test_vector_migration_populates_format_and_dimension(store):
    conn, tmp, _ = store
    seed_v8_vectors(conn, [[0.1, 0.2], [0.3, 0.4]])
    conn.close()
    migrated = get_connection(tmp / 'target.db')
    try:
        config = dict(migrated.execute('SELECT * FROM memory_semantic_config').fetchone())
        assert config.get('format') == 'f32le-v1'
        assert config.get('dimension') == 2
        assert {r[0] for r in migrated.execute('SELECT typeof(vector) FROM memory_vectors')} == {'blob'}
    finally:
        migrated.close()


@pytest.mark.parametrize('malformed', ['12', {'1': 99, '2': 88}])
def test_vector_migration_rejects_json_that_is_not_a_numeric_array(store, malformed):
    conn, _, _ = store
    seed_v8_vectors(conn, [malformed, [0.1, 0.2]])
    converted, dropped = memory.migrate_vectors_to_blob(conn)
    assert (converted, dropped) == (1, 1)


def test_compaction_preserves_tail_and_python_offset_after_embedded_nul(store):
    from post_compact import build_recovery_context, RECOVERY_BLOCK_CHARS
    conn, tmp, path = store
    text = 'prefix\x00' + 'x' * 1000 + 'ACTUAL_FINAL_CONCLUSION'
    trace(path, [record('Opening ask', 'a', 'user'), record(text, 'b')])
    memory.index_file(conn, path, session_id='round3', cwd=str(tmp), rebuild=True)
    conn.commit()
    output = build_recovery_context(conn, 'round3')
    assert 'ACTUAL_FINAL_CONCLUSION' in output
    assert f'--start {len(text) - RECOVERY_BLOCK_CHARS}' in output


def test_rebuild_renumbers_across_capped_passes_and_repeated_message_ids(store):
    conn, tmp, path = store
    records = [record('New opening', 'new', 'user'), record('updated sentinel evidence', 'a'),
               record('second passage evidence', 'b'), record('updated sentinel continuation', 'a'),
               record('last message', 'last')]
    trace(path, records)
    result = memory.index_file(conn, path, session_id='round3', cwd=str(tmp), rebuild=True, max_records=1)
    for _ in range(10):
        if result['state'] == 'complete':
            break
        result = memory.index_file(conn, path, session_id='round3', cwd=str(tmp), max_records=1)
    assert result['state'] == 'complete'
    rows = conn.execute('SELECT message_key, seq FROM memory_blocks ORDER BY seq,ordinal').fetchall()
    assert [tuple(r) for r in rows] == [('new', 1), ('a', 2), ('b', 3), ('last', 4)]
    plan = conn.execute('EXPLAIN QUERY PLAN SELECT seq,generation FROM memory_blocks '
                        'INDEXED BY memory_blocks_message WHERE source_key=? AND message_key=?',
                        ('claude:round3', 'a')).fetchall()
    assert any('memory_blocks_message (source_key=? AND message_key=?)' in r[3] for r in plan)


def test_p52_atomic_view_removes_stale_chunks_and_vectors_only_at_publication(store):
    conn, tmp, path = store
    conn.execute("INSERT INTO memory_vectors(chunk_id,model,vector) SELECT id,'model',x'0000803f' FROM memory_chunks")
    conn.commit()
    # Published a and b, including their vectors, remain until atomic publication.
    trace(path, [record('replacement sentinel evidence', 'a', 'user'), record('new last evidence', 'c')])
    result = memory.index_file(conn, path, session_id='round3', cwd=str(tmp), rebuild=True, max_records=1)
    assert result['state'] == 'rebuilding'
    assert memory.search(conn, 'irreplaceable')
    assert not memory.search(conn, 'replacement') and memory.search(conn, 'second')
    assert conn.execute('SELECT count(*) FROM memory_vectors').fetchone()[0] == 2
    result = memory.index_file(conn, path, session_id='round3', cwd=str(tmp))
    assert result['state'] == 'complete' and result['stale_removed'] == 1
    assert not memory.search(conn, 'second')
    assert conn.execute('SELECT count(*) FROM memory_vectors').fetchone()[0] == 0
    assert conn.execute('PRAGMA foreign_key_check').fetchall() == []


def test_vector_conversion_rollback_can_be_retried(store):
    conn, tmp, _ = store
    seed_v8_vectors(conn, [[0.1, 0.2], [0.3, 0.4]])
    assert memory.migrate_vectors_to_blob(conn) == (2, 0)
    conn.rollback()
    assert {r[0] for r in conn.execute('SELECT typeof(vector) FROM memory_vectors')} == {'text'}
    assert memory.migrate_vectors_to_blob(conn) == (2, 0)
    conn.commit()
    assert memory.migrate_vectors_to_blob(conn) == (0, 0)
