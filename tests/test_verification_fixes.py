"""Fable's regression tests for the integration-verification fixes: pristine-derived
schema verification (P53), host-metadata handling in brief and compaction (P54), and the
Codex skill location (P55). Temporary stores only."""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'hooks')]
import memory_store as memory
from db import get_connection
from recall_memory import parser, run


def rec(text, key, role='assistant'):
    return {'type': role, 'uuid': key, 'sessionId': 'v', 'timestamp': '2026-09-06T00:00:00Z',
            'message': {'role': role, 'content': [{'type': 'text', 'text': text}]}}


def write(path, *records):
    path.write_text(''.join(json.dumps(r) + '\n' for r in records))


@pytest.fixture
def store(tmp_path):
    c = get_connection(tmp_path / 'store.db')
    p = tmp_path / 'trace.jsonl'
    write(p, rec('irreplaceable evidence', 'a', 'user'), rec('second passage', 'b'))
    memory.index_file(c, p, session_id='v', cwd=str(tmp_path)); c.commit()
    yield c, tmp_path, p
    c.close()


# ---------------------------------------------------------------- P53
def test_expected_schema_is_read_from_a_pristine_store():
    tables, indexes, triggers = memory.expected_schema()
    assert {'timestamp', 'text', 'generation', 'ordinal'} <= tables['memory_blocks']
    assert {'scope_pinned', 'head_hash'} <= tables['memory_sources']
    assert {'memory_chunks_insert', 'memory_chunks_delete'} <= triggers
    assert 'memory_blocks_message' in indexes and 'idx_sessions_project' in indexes


@pytest.mark.parametrize('damage,expected', [
    ('ALTER TABLE memory_blocks DROP COLUMN timestamp', 'missing column memory_blocks.timestamp'),
    ('DROP TRIGGER memory_chunks_insert', 'missing trigger memory_chunks_insert'),
    ("INSERT INTO memory_fts(memory_fts) VALUES('delete-all')", 'memory_fts unusable or out of sync'),
    ("INSERT INTO exchanges_fts(exchanges_fts) VALUES('delete-all')", 'exchanges_fts unusable or out of sync'),
    ('DROP INDEX idx_tags_tag', 'missing index idx_tags_tag'),
])
def test_verifier_names_each_kind_of_damage(store, damage, expected):
    c, _, p = store
    from prompt_submit import index_transcript
    index_transcript(c, 'v', str(p), '/x', 'h'); c.commit()   # exchanges_fts has content to be out of sync with
    c.execute(damage); c.commit()
    assert any(problem.startswith(expected) for problem in memory.verify_schema(c)), memory.verify_schema(c)


def test_repair_recreates_triggers_indexes_and_fts_but_not_columns(store):
    c, _, p = store
    from prompt_submit import index_transcript
    index_transcript(c, 'v', str(p), '/x', 'h'); c.commit()
    c.execute('DROP TRIGGER memory_chunks_insert'); c.execute('DROP INDEX idx_tags_tag')
    c.execute("INSERT INTO memory_fts(memory_fts) VALUES('delete-all')"); c.execute("INSERT INTO exchanges_fts(exchanges_fts) VALUES('delete-all')"); c.commit()
    assert not memory.search(c, 'irreplaceable')
    fixed = run(parser().parse_args(['doctor', '--repair']), c)
    assert fixed['schema_check'] == 'ok' and len(fixed['repaired']) == 4
    assert memory.search(c, 'irreplaceable')
    c.execute('ALTER TABLE memory_blocks DROP COLUMN start_byte'); c.commit()
    assert run(parser().parse_args(['doctor', '--repair']), c)['schema_check'] == ['missing column memory_blocks.start_byte']


def test_restore_and_doctor_agree_on_a_desynced_fts_backup(store):
    c, tmp, _ = store
    backup = tmp / 'b.db'
    run(parser().parse_args(['backup', str(backup)]), c)
    o = sqlite3.connect(backup); o.execute("INSERT INTO memory_fts(memory_fts) VALUES('delete-all')"); o.commit(); o.close()
    with pytest.raises(ValueError, match='out of sync'):
        run(parser().parse_args(['restore', str(backup), '--yes']), c)
    assert memory.search(c, 'irreplaceable')


# ---------------------------------------------------------------- P54
PLUGINS = '<recommended_plugins>\nHere is a list of plugins.\n- Airtable (airtable@remote)\n</recommended_plugins>'
REMINDER = '<system-reminder>\nThe user has a memory directory.\n</system-reminder>'


def test_prose_segments_strip_wrappers_and_keep_offsets():
    assert memory.prose_segments(PLUGINS) == [] and memory.is_host_metadata(REMINDER)
    mixed = REMINDER + '\n\nPlease fix the login bug\n' + PLUGINS
    (a, b), = memory.prose_segments(mixed)
    assert mixed[a:b] == 'Please fix the login bug'
    assert memory.prose_segments('plain ask') == [(0, 9)]
    assert memory.prose_segments('<system-reminder>unterminated') == [(0, 29)]


def test_brief_skips_metadata_blocks_and_excerpts_the_users_words(store):
    c, tmp, p = store
    mixed = REMINDER + '\n\nDecide whether to keep batching\n' + PLUGINS
    write(p, rec(PLUGINS, 'meta', 'user'), rec(mixed, 'ask', 'user'), rec('we rejected batching because of latency', 'ans'))
    memory.index_file(c, p, session_id='v', cwd=str(tmp), rebuild=True); c.commit()
    out = memory.brief(c, source_key='claude:v')
    ids = [e['id'] for e in out['evidence']]
    meta_id = memory.digest('claude:v:meta:0:text')[:32]
    assert meta_id not in ids
    opening = out['evidence'][0]
    assert opening['role'] == 'user' and opening['host_metadata_stripped'] is True
    ex = opening['excerpts'][0]
    assert ex['text'] == 'Decide whether to keep batching' and mixed[ex['start_char']:].startswith('Decide')


def test_compaction_skips_metadata_opening_and_cites_prose_offset(store):
    from db import insert_session
    from post_compact import build_recovery_context
    c, tmp, p = store
    mixed = REMINDER + '\n\nReal objective here\n' + PLUGINS
    write(p, rec(PLUGINS, 'meta', 'user'), rec(mixed, 'ask', 'user'), rec('answer text', 'x'), rec(PLUGINS, 'meta2', 'user'), rec('final conclusion', 'y'))
    memory.index_file(c, p, session_id='v', cwd=str(tmp), rebuild=True); c.commit()
    insert_session(c, 'v', str(tmp), 'h', '2026-09-06')
    out = build_recovery_context(c, 'v')
    assert 'Airtable' not in out and 'memory directory' not in out
    assert f"--start {mixed.index('Real objective')}" in out and 'Real objective here' in out
    assert 'final conclusion' in out and 'answer text' in out


# ---------------------------------------------------------------- P55
def test_codex_skill_installer_defaults_to_agents_skills():
    assert parser().parse_args(['install-codex-skill']).skills_dir == '~/.agents/skills'


# ---------------------------------------------------------------- P54 follow-up (review of the real brief)
def test_brief_flag_only_when_a_wrapper_was_removed_and_short_blocks_are_one_excerpt(store):
    c, tmp, p = store
    short = '\n' + 'word ' * 150 + 'END'          # 753 chars of prose with a leading newline
    write(p, rec('Get caught up on this:\n', 'ask', 'user'), rec(short, 'reply'), rec(REMINDER + '\nreal words', 'mixed', 'user'))
    memory.index_file(c, p, session_id='v', cwd=str(tmp), rebuild=True); c.commit()
    by_id = {e['id']: e for e in memory.brief(c, source_key='claude:v')['evidence']}
    ask = by_id[memory.digest('claude:v:ask:0:text')[:32]]
    assert 'host_metadata_stripped' not in ask and ask['excerpts'] == [{'start_char': 0, 'text': 'Get caught up on this:'}]
    reply = by_id[memory.digest('claude:v:reply:0:text')[:32]]
    assert 'host_metadata_stripped' not in reply and len(reply['excerpts']) == 1
    assert reply['excerpts'][0]['start_char'] == 1 and reply['excerpts'][0]['text'].endswith('END')
    mixed = by_id[memory.digest('claude:v:mixed:0:text')[:32]]
    assert mixed['host_metadata_stripped'] is True and mixed['excerpts'][0]['text'] == 'real words'


def test_brief_long_blocks_keep_head_and_tail_inside_prose(store):
    c, tmp, p = store
    long = 'A' * 1200 + REMINDER + 'B' * 1200
    write(p, rec('ask', 'a', 'user'), rec(long, 'l'))
    memory.index_file(c, p, session_id='v', cwd=str(tmp), rebuild=True); c.commit()
    e = {x['id']: x for x in memory.brief(c, source_key='claude:v')['evidence']}[memory.digest('claude:v:l:0:text')[:32]]
    assert [len(x['text']) for x in e['excerpts']] == [500, 500]
    assert set(e['excerpts'][0]['text']) == {'A'} and set(e['excerpts'][1]['text']) == {'B'}
    assert e['excerpts'][1]['start_char'] == len(long) - 500 and e['host_metadata_stripped'] is True
