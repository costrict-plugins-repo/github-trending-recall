"""Fable's regression tests for the round-2 fixes (R01-R04, R06-R11, P14), beyond
Astra's reproducers in test_astra_round2.py. Temporary stores only."""
import json
import math
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'hooks')]
import memory_store as memory
from db import get_connection, SCHEMA_VERSION
from recall_memory import parser, run
from utils import redact_secrets


def rec(text, key, role='assistant', cwd=None, **extra):
    value = {'type': role, 'uuid': key, 'timestamp': '2026-09-05T12:00:00Z', 'sessionId': 'fx',
             'message': {'role': role, 'content': [{'type': 'text', 'text': text}]}}
    if cwd:
        value['cwd'] = str(cwd)
    value.update(extra)
    return value


def write(path, *records):
    path.write_text(''.join(json.dumps(r) + '\n' for r in records))


@pytest.fixture
def store(tmp_path):
    c = get_connection(tmp_path / 'store.db')
    yield c, tmp_path
    c.close()


# ---------------------------------------------------------------- R01
@pytest.mark.parametrize('text', [
    'api_key' + ' ' * 256_000 + '!',
    'password' + ' ' * 256_000 + 'is',
    'api_key' + '"' * 100_000 + '=',
    'token' + ' \t' * 100_000 + ':',
    ('secret_key = ' + ' ' * 5000) * 40,
    'bearer' + ' ' * 200_000 + 'x',
    'x' * 400_000,
])
def test_redaction_is_linear_on_adversarial_whitespace_and_quotes(text):
    t = time.perf_counter()
    redact_secrets(text)
    assert time.perf_counter() - t < 0.5


@pytest.mark.parametrize('text,leaked', [
    ('api_key = "abcdefghijklmnop"', 'abcdefghijklmnop'),
    ("password: 'hunter2hunter2'", 'hunter2hunter2'),
    ('"api_key": "abcdefghijkl"', 'abcdefghijkl'),
    ('AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY', 'wJalr'),
    ('my password is hunter2-hunter2', 'hunter2-hunter2'),
    ('the api key was    abcdefghijklmno', 'abcdefghijklmno'),
    ('Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123', 'abcdefghijklmnopqrstuvwxyz0123'),
])
def test_redaction_still_catches_common_shapes(text, leaked):
    out = redact_secrets(text)
    assert leaked not in out and 'REDACTED' in out


# ---------------------------------------------------------------- R02
def _seed(store):
    c, tmp = store
    p = tmp / 'trace.jsonl'
    write(p, rec('irreplaceable evidence', 'a'))
    memory.index_file(c, p, session_id='fx', cwd=str(tmp)); c.commit()
    return c, tmp


def _still_intact(c):
    assert c.execute("SELECT text FROM memory_blocks").fetchone()[0] == 'irreplaceable evidence'
    assert c.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION


def test_restore_rejects_a_newer_schema_without_touching_the_target(store):
    c, tmp = _seed(store)
    future = tmp / 'future.db'
    other = get_connection(future); other.execute('PRAGMA user_version=%d' % (SCHEMA_VERSION + 5)); other.commit(); other.close()
    with pytest.raises(ValueError, match='newer'):
        run(parser().parse_args(['restore', str(future), '--yes']), c)
    _still_intact(c)


def test_restore_rejects_the_store_itself(store):
    c, tmp = _seed(store)
    with pytest.raises(ValueError, match='same file'):
        run(parser().parse_args(['restore', str(tmp / 'store.db'), '--yes']), c)
    _still_intact(c)


def test_restore_rejects_non_recall_and_corrupt_files(store):
    c, tmp = _seed(store)
    other = sqlite3.connect(tmp / 'other.db'); other.execute('CREATE TABLE unrelated(v)'); other.commit(); other.close()
    with pytest.raises(ValueError, match='Not a Recall store'):
        run(parser().parse_args(['restore', str(tmp / 'other.db'), '--yes']), c)
    (tmp / 'garbage.db').write_bytes(b'not a database at all' * 100)
    with pytest.raises((ValueError, sqlite3.DatabaseError)):
        run(parser().parse_args(['restore', str(tmp / 'garbage.db'), '--yes']), c)
    _still_intact(c)


def test_restore_of_a_valid_older_backup_still_works(store):
    c, tmp = _seed(store)
    backup = tmp / 'good.db'
    run(parser().parse_args(['backup', str(backup)]), c)
    src = sqlite3.connect(backup); src.execute('PRAGMA user_version=5'); src.commit(); src.close()   # pretend it is old
    result = run(parser().parse_args(['restore', str(backup), '--yes']), c)
    assert result['schema_version'] == SCHEMA_VERSION and result['integrity_check'] == 'ok'
    _still_intact(c)


# ---------------------------------------------------------------- R04
def test_import_export_uses_the_source_generation_not_the_file_value(store):
    c, tmp = store
    p = tmp / 'trace.jsonl'; write(p, rec('kept', 'a'))
    memory.index_file(c, p, session_id='fx', cwd=str(tmp)); c.commit()
    export = run(parser().parse_args(['export', 'claude:fx']), c)
    for b in export['blocks']:
        b['generation'] = 7   # a stale or hand-edited export must not leak a higher generation
    path = tmp / 'e.json'; path.write_text(json.dumps(export))
    run(parser().parse_args(['prune', 'claude:fx']), c)
    run(parser().parse_args(['import-export', str(path)]), c); c.commit()
    src_gen = c.execute("SELECT generation FROM memory_sources WHERE source_key='claude:fx'").fetchone()[0]
    assert {r[0] for r in c.execute("SELECT generation FROM memory_blocks")} == {src_gen}


# ---------------------------------------------------------------- R07
def test_rescope_pin_survives_index_passes_and_auto_unpins(store):
    c, tmp = store
    observed = tmp / 'observed'; corrected = tmp / 'corrected'; observed.mkdir(); corrected.mkdir()
    p = tmp / 'trace.jsonl'; write(p, rec('evidence', 'a', cwd=observed))
    memory.index_file(c, p); c.commit()
    pinned = run(parser().parse_args(['rescope', 'claude:fx', '--cwd', str(corrected)]), c); c.commit()
    write(p, rec('evidence', 'a', cwd=observed), rec('more', 'b', cwd=observed))
    memory.index_file(c, p); c.commit()   # new records, observed cwd still the old one
    row = c.execute("SELECT repo_id, project_path, scope_pinned FROM memory_sources WHERE source_key='claude:fx'").fetchone()
    assert (row[0], row[1], row[2]) == (pinned['now']['repo_id'], pinned['now']['project_path'], 1)
    assert run(parser().parse_args(['rescope', 'claude:fx', '--auto']), c)['pinned'] is False
    memory.index_file(c, p); c.commit()
    assert c.execute("SELECT project_path FROM memory_sources").fetchone()[0] == str(observed)
    with pytest.raises(ValueError):
        run(parser().parse_args(['rescope', 'claude:fx']), c)


# ---------------------------------------------------------------- R09
@pytest.mark.parametrize('shape', [
    {'message': {'role': 'assistant', 'content': 7}},
    {'message': {'role': 'assistant', 'content': {'type': 'text', 'text': 'obj'}}},
    {'message': {'role': 'assistant', 'content': [None, 3, 'str', {'type': 'text', 'text': None}]}},
    {'message': {'role': 'assistant', 'content': [{'type': 'tool_use', 'name': None, 'input': 'a string, not a dict'}]}},
    {'message': {'role': 'assistant', 'content': [{'type': 'tool_use', 'name': 3, 'input': None}]}},
    {'message': {'role': 'user', 'content': [{'type': 'text', 'text': 'ok'}]}, 'timestamp': None},
    {'message': 'not a dict'},
    {'message': {'role': 'assistant', 'content': [{'type': 'tool_use', 'name': 'x', 'input': {'k': {'nested': [1, 2]}}}]}},
])
def test_odd_record_shapes_never_wedge_the_pass(store, shape):
    c, tmp = store
    p = tmp / 'trace.jsonl'
    bad = {'type': 'assistant', 'uuid': 'bad', 'timestamp': '2026-09-05T12:00:00Z', **shape}
    write(p, rec('before evidence', 'a'), bad, rec('after evidence', 'b'))
    result = memory.index_file(c, p, session_id='fx', cwd=str(tmp)); c.commit()
    assert result['state'] == 'complete'
    assert memory.search(c, 'after') and memory.search(c, 'before')


# ---------------------------------------------------------------- R10
def test_compaction_excerpts_carry_exact_start_offsets(store):
    from db import insert_session
    from post_compact import build_recovery_context, RECOVERY_BLOCK_CHARS, RECOVERY_OBJECTIVE_CHARS
    c, tmp = store
    p = tmp / 'trace.jsonl'
    long_open = 'O' * 1000
    long_tail = 'x' * 2000 + 'CONCLUSION'
    write(p, rec(long_open, 'a', 'user'), rec('short middle', 'b'), rec(long_tail, 'c'))
    memory.index_file(c, p, session_id='fx', cwd=str(tmp)); c.commit()
    insert_session(c, 'fx', str(tmp), 'hash', '2026-09-05')
    text = build_recovery_context(c, 'fx')
    assert text.count('O') >= RECOVERY_OBJECTIVE_CHARS and 'O' * (RECOVERY_OBJECTIVE_CHARS + 1) not in text
    assert 'CONCLUSION' in text
    assert f'--start {len(long_tail) - RECOVERY_BLOCK_CHARS}' in text
    assert '--start 0' in text and len(text) <= 3500


# ---------------------------------------------------------------- R11
def test_coverage_follows_the_source_filter(store):
    c, tmp = store
    p = tmp / 'trace.jsonl'; write(p, rec('evidence', 'a'))
    memory.index_file(c, p, session_id='fx', cwd=str(tmp)); c.commit()
    assert memory.status(c, source_key='claude:fx')['source_count'] == 1
    assert memory.status(c, source_key='claude:none')['source_count'] == 0
    brief = run(parser().parse_args(['brief', '--source', 'claude:none']), c)
    assert brief['coverage']['source_count'] == 0


# ---------------------------------------------------------------- P14
def test_vector_pack_roundtrip_is_exact_and_rejects_bad_input():
    from semantic_memory import pack_vector, unpack_vector
    import random
    values = [random.uniform(-1, 1) for _ in range(384)]
    blob = pack_vector(values)
    assert len(blob) == 1536
    back = unpack_vector(blob)
    import struct
    assert list(back) == list(struct.unpack('<384f', struct.pack('<384f', *values)))
    assert unpack_vector(json.dumps([1.0, 2.5])) == (1.0, 2.5)
    for bad in ([], [1.0, float('nan')], [float('inf')], ['x']):
        with pytest.raises((ValueError, TypeError)):
            pack_vector(bad)
    with pytest.raises(ValueError):
        unpack_vector(b'abc')


def test_pre_v9_json_vectors_migrate_or_are_dropped(store):
    c, tmp = store
    p = tmp / 'trace.jsonl'; write(p, *[rec('passage %d' % i, str(i)) for i in range(5)])
    memory.index_file(c, p, session_id='fx', cwd=str(tmp)); c.commit()
    chunks = [r[0] for r in c.execute('SELECT id FROM memory_chunks ORDER BY id')]
    rows = [(chunks[0], 'm', json.dumps([0.1, 0.2, 0.3])), (chunks[1], 'm', json.dumps([0.4, 0.5, 0.6])),
            (chunks[2], 'm', json.dumps([1.0, 2.0])),           # wrong dimension for model m
            (chunks[3], 'm', 'not json'), (chunks[4], 'm', json.dumps([1.0, None, 2.0]))]
    c.executemany('INSERT INTO memory_vectors(chunk_id,model,vector) VALUES(?,?,?)', rows); c.commit()
    converted, dropped = memory.migrate_vectors_to_blob(c); c.commit()
    assert (converted, dropped) == (2, 3)
    kept = c.execute("SELECT chunk_id, typeof(vector), length(vector) FROM memory_vectors ORDER BY chunk_id").fetchall()
    assert [tuple(r) for r in kept] == [(chunks[0], 'blob', 12), (chunks[1], 'blob', 12)]
    assert memory.migrate_vectors_to_blob(c) == (0, 0)   # idempotent


def test_schema_9_migration_from_a_schema_7_copy_adds_indexes_and_column(tmp_path):
    db = tmp_path / 'v7.db'
    c = get_connection(db)
    c.execute('DROP INDEX IF EXISTS memory_blocks_generation'); c.execute('DROP INDEX IF EXISTS memory_blocks_message')
    c.execute('PRAGMA user_version=7'); c.commit(); c.close()
    c = get_connection(db)
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {'memory_blocks_generation', 'memory_blocks_message'} <= names
    assert 'scope_pinned' in {r[1] for r in c.execute('PRAGMA table_info(memory_sources)')}
    assert c.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION
    c.close()
