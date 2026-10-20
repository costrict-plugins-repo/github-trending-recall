"""Fable's regression tests for the round-3 fixes (R3-01..R3-05), beyond Astra's
reproducers in test_astra_round3.py. Temporary stores only."""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'hooks')]
import memory_store as memory
from db import get_connection, SCHEMA_VERSION
from recall_memory import parser, run
from semantic_memory import pack_vector, unpack_vector, VECTOR_FORMAT


def rec(text, key, role='assistant'):
    return {'type': role, 'uuid': key, 'sessionId': 'r3', 'timestamp': '2026-09-05T12:00:00Z',
            'message': {'role': role, 'content': [{'type': 'text', 'text': text}]}}


def write(path, *records):
    path.write_text(''.join(json.dumps(r) + '\n' for r in records))


@pytest.fixture
def store(tmp_path):
    c = get_connection(tmp_path / 'store.db')
    p = tmp_path / 'trace.jsonl'
    write(p, rec('irreplaceable evidence', 'a', 'user'), rec('second block', 'b'))
    memory.index_file(c, p, session_id='r3', cwd=str(tmp_path)); c.commit()
    yield c, tmp_path, p
    c.close()


# ---------------------------------------------------------------- R3-01
def test_fresh_store_verifies_clean(store):
    c, _, _ = store
    assert memory.verify_schema(c) == []


@pytest.mark.parametrize('damage', ['DROP INDEX memory_blocks_message', 'DROP INDEX memory_blocks_generation',
                                    'DROP TABLE memory_fts', 'DROP TABLE exchanges_fts'])
def test_restore_rejects_backups_missing_derived_objects(store, damage):
    c, tmp, _ = store
    backup = tmp / 'backup.db'
    run(parser().parse_args(['backup', str(backup)]), c)
    other = sqlite3.connect(backup); other.execute(damage); other.commit(); other.close()
    with pytest.raises(ValueError, match='not a complete Recall store'):
        run(parser().parse_args(['restore', str(backup), '--yes']), c)
    assert memory.search(c, 'irreplaceable') and memory.verify_schema(c) == []


def test_restore_rejects_backups_missing_base_tables(store):
    c, tmp, _ = store
    backup = tmp / 'backup.db'
    run(parser().parse_args(['backup', str(backup)]), c)
    other = sqlite3.connect(backup); other.execute('DROP TABLE memory_blocks'); other.commit(); other.close()
    with pytest.raises((ValueError, sqlite3.DatabaseError)):
        run(parser().parse_args(['restore', str(backup), '--yes']), c)
    assert memory.search(c, 'irreplaceable')


def test_doctor_reports_and_repairs_derived_objects(store):
    c, _, _ = store
    c.execute('DROP INDEX memory_blocks_message'); c.execute('DROP TABLE memory_fts'); c.commit()
    report = run(parser().parse_args(['doctor']), c)
    assert 'missing index memory_blocks_message' in report['schema_check'] and 'missing table memory_fts' in report['schema_check']
    repaired = run(parser().parse_args(['doctor', '--repair']), c)
    assert {'missing index memory_blocks_message', 'missing table memory_fts'} <= set(repaired['repaired'])
    assert repaired['schema_check'] == 'ok' and memory.search(c, 'irreplaceable')


# ---------------------------------------------------------------- R3-04
@pytest.mark.parametrize('name', ['backup?#copy.db', 'backup with space.db', 'back%20up.db', "it's.db", 'plain.db'])
def test_restore_addresses_literal_filenames(store, name):
    c, tmp, _ = store
    backup = tmp / name
    run(parser().parse_args(['backup', str(backup)]), c)
    assert backup.is_file()
    result = run(parser().parse_args(['restore', str(backup), '--yes']), c)
    assert result['integrity_check'] == 'ok' and memory.search(c, 'irreplaceable')
    # exactly the selected file was addressed: no URI-mangled sibling was created
    assert [q.name for q in tmp.iterdir() if q.suffix == '.db' and q.name not in ('store.db', name)] == []


# ---------------------------------------------------------------- R3-02
@pytest.mark.parametrize('bad', ['12', {'1': 99}, [], [1e100, 0.0], [float('nan')], [True, False], ['1', '2'], [[1.0]], b'\x00\x00\x80?'])
def test_pack_vector_rejects_non_numeric_or_unrepresentable(bad):
    with pytest.raises(ValueError):
        pack_vector(bad)


def test_pack_vector_accepts_ints_and_float32_extremes():
    blob = pack_vector([1, -2, 3.4028234663852886e38, -3.4028234663852886e38, 0])
    assert len(blob) == 20 and unpack_vector(blob)[0] == 1.0


def _seed_v8(c, values, with_config=True):
    ids = [r[0] for r in c.execute('SELECT id FROM memory_chunks ORDER BY id')]
    if with_config:
        c.execute('CREATE TABLE memory_semantic_config(id INTEGER PRIMARY KEY CHECK(id=1), path TEXT, model TEXT)')
        c.execute("INSERT INTO memory_semantic_config VALUES(1,'/offline/model','fp')")
    c.executemany('INSERT INTO memory_vectors(chunk_id,model,vector) VALUES(?,?,?)',
                  [(i, 'fp', json.dumps(v)) for i, v in zip(ids, values)])
    c.execute('PRAGMA user_version=8'); c.commit()


def test_migration_never_aborts_and_drops_every_invalid_shape(store):
    c, tmp, _ = store
    _seed_v8(c, [[1e100, 0.0], [0.1, 0.2]])
    c.close()
    m = get_connection(tmp / 'store.db')
    assert m.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION
    assert [tuple(r) for r in m.execute("SELECT typeof(vector), length(vector) FROM memory_vectors")] == [('blob', 8)]
    assert memory.search(m, 'irreplaceable')
    m.close()


# ---------------------------------------------------------------- R3-03
def test_migration_records_format_and_dimension_or_null_for_empty_index(tmp_path):
    for values in ([[0.1, 0.2]], []):
        db = tmp_path / f'{len(values)}.db'
        c = get_connection(db); p = tmp_path / 't.jsonl'; write(p, rec('x', 'a')); memory.index_file(c, p, session_id='s', cwd=str(tmp_path)); c.commit()
        _seed_v8(c, values); c.close()
        m = get_connection(db)
        cfg = dict(m.execute('SELECT * FROM memory_semantic_config').fetchone())
        assert cfg['format'] == VECTOR_FORMAT and cfg['dimension'] == (2 if values else None)
        m.close()


def test_mixed_dimensions_follow_the_first_valid_row(store):
    c, tmp, _ = store
    _seed_v8(c, [[0.1, 0.2], [0.1, 0.2, 0.3]])
    c.close()
    m = get_connection(tmp / 'store.db')
    assert tuple(m.execute('SELECT count(*), min(length(vector)) FROM memory_vectors').fetchone()) == (1, 8)
    assert m.execute('SELECT dimension FROM memory_semantic_config').fetchone()[0] == 2
    m.close()


def test_blob_rows_disagreeing_with_the_dimension_are_dropped(store):
    c, _, _ = store
    ids = [r[0] for r in c.execute('SELECT id FROM memory_chunks ORDER BY id')]
    c.execute('INSERT INTO memory_vectors VALUES(?,?,?)', (ids[0], 'fp', sqlite3.Binary(pack_vector([1.0, 2.0]))))
    c.execute('INSERT INTO memory_vectors VALUES(?,?,?)', (ids[1], 'fp', sqlite3.Binary(pack_vector([1.0]))))
    assert memory.migrate_vectors_to_blob(c) == (0, 1)
    assert c.execute('SELECT count(*) FROM memory_vectors').fetchone()[0] == 1


# ---------------------------------------------------------------- R3-05
def test_nul_in_opening_block_keeps_head_and_total(store):
    from db import insert_session
    from post_compact import build_recovery_context, RECOVERY_OBJECTIVE_CHARS
    c, tmp, p = store
    opening = 'ask\x00' + 'o' * 900
    write(p, rec(opening, 'a', 'user'), rec('reply', 'b'))
    memory.index_file(c, p, session_id='r3', cwd=str(tmp), rebuild=True); c.commit()
    insert_session(c, 'r3', str(tmp), 'h', '2026-09-05')
    out = build_recovery_context(c, 'r3')
    assert out.count('o') >= RECOVERY_OBJECTIVE_CHARS - 4 and '…' in out.split('Most recent')[0]


def test_no_nul_path_is_unchanged(store):
    from db import insert_session
    from post_compact import build_recovery_context, RECOVERY_BLOCK_CHARS
    c, tmp, p = store
    tail = 'y' * 2000 + 'END'
    write(p, rec('ask', 'a', 'user'), rec(tail, 'b'))
    memory.index_file(c, p, session_id='r3', cwd=str(tmp), rebuild=True); c.commit()
    insert_session(c, 'r3', str(tmp), 'h', '2026-09-05')
    out = build_recovery_context(c, 'r3')
    assert 'END' in out and f'--start {len(tail) - RECOVERY_BLOCK_CHARS}' in out
