"""P65/P64 (Fable, wrap-up review): Claude host records that are not the user's words, and
MCP error wording. Found on a real compaction receipt: the hook cited the rendered skill
body as 'most recent' user context. Temporary stores only."""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'hooks')]
import memory_store as memory
from db import get_connection, insert_session


def rec(text, key, role='assistant', **flags):
    return {'type': role, 'uuid': key, 'sessionId': 'h', 'timestamp': '2026-09-06T00:00:00Z',
            'message': {'role': role, 'content': [{'type': 'text', 'text': text}]}, **flags}


SKILL_BODY = 'Base directory for this skill: /x/skills/recall\n\n# Context Recall\n\nThe user wants to recover context.\n' + 'instructions ' * 200
SUMMARY = 'This session is being continued from a previous conversation that ran out of context. Summary: the user decided on amber-sparrow.'


@pytest.fixture
def store(tmp_path):
    c = get_connection(tmp_path / 'store.db')
    p = tmp_path / 'trace.jsonl'
    p.write_text(''.join(json.dumps(r) + '\n' for r in [
        rec('Real opening ask about the delivery label', 'ask', 'user'),
        rec('I will search past sessions.', 'a1'),
        rec(SKILL_BODY, 'skill', 'user', isMeta=True, sourceToolUseID='toolu_1'),
        rec(SUMMARY, 'sum', 'user', isCompactSummary=True, isVisibleInTranscriptOnly=True),
        rec('Final answer: amber-sparrow, cited.', 'a2'),
    ]))
    result = memory.index_file(c, p, session_id='h', cwd=str(tmp_path)); c.commit()
    insert_session(c, 'h', str(tmp_path), 'hash', '2026-09-06')
    yield c, result
    c.close()


def test_skill_body_is_excluded_by_policy_and_counted(store):
    c, result = store
    texts = [r[0] for r in c.execute('SELECT text FROM memory_blocks')]
    assert not any('Base directory for this skill' in t for t in texts)
    row = c.execute("SELECT excluded FROM memory_sources WHERE source_key='claude:h'").fetchone()
    assert row[0] == 1
    assert memory.classify_skipped(rec(SKILL_BODY, 'skill', 'user', isMeta=True), 'claude') == 'excluded'


def test_compact_summary_is_retained_as_host_and_searchable_but_never_quoted(store):
    c, _ = store
    assert c.execute("SELECT role FROM memory_blocks WHERE message_key='sum'").fetchone()[0] == 'host'
    assert any(h['role'] == 'host' for h in memory.search(c, 'continued from a previous conversation'))
    brief = memory.brief(c, source_key='claude:h')
    assert all(e['role'] in ('user', 'assistant') for e in brief['evidence'])
    assert brief['evidence'][0]['excerpts'][0]['text'].startswith('Real opening ask')
    from post_compact import build_recovery_context
    out = build_recovery_context(c, 'h')
    assert 'Real opening ask' in out and 'Final answer' in out
    assert 'Base directory' not in out and 'continued from a previous conversation' not in out


def test_codex_records_are_unaffected_by_claude_flags(tmp_path):
    c = get_connection(tmp_path / 's.db'); p = tmp_path / 'r.jsonl'
    p.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': 'cx', 'cwd': str(tmp_path)}}) + '\n' +
                 json.dumps({'type': 'response_item', 'isMeta': True, 'payload': {'type': 'message', 'id': 'm', 'role': 'user',
                             'content': [{'type': 'input_text', 'text': 'codex words'}]}}) + '\n')
    memory.index_file(c, p, agent='codex'); c.commit()
    assert tuple(c.execute('SELECT role, text FROM memory_blocks').fetchone()) == ('user', 'codex words')
    c.close()


@pytest.mark.parametrize('exc,expected', [
    (sqlite3.OperationalError('interrupted'), '2-second budget'),
    (sqlite3.OperationalError('database is locked'), 'busy'),
    (sqlite3.DatabaseError('file is not a database'), 'needs maintenance'),
    (ValueError('Unknown block in this repository'), 'Unknown block'),
])
def test_mcp_error_messages_name_the_cause(tmp_path, monkeypatch, exc, expected):
    import recall_mcp
    c = get_connection(tmp_path / 'store.db'); c.close()
    proto = recall_mcp.Protocol(recall_mcp.RecallService(tmp_path / 'store.db', 'directory:test'))
    proto.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-11-25', 'capabilities': {}, 'clientInfo': {'name': 't', 'version': '0'}}})
    proto.handle({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
    monkeypatch.setattr(recall_mcp.RecallService, 'call', lambda self, name, args: (_ for _ in ()).throw(exc))
    reply = proto.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'recall_search', 'arguments': {'query': 'x'}}})
    assert reply['result']['isError'] and expected in reply['result']['content'][0]['text']


@pytest.mark.parametrize('changed_text', [False, True])
def test_rebuild_corrects_existing_summary_role_and_excludes_old_skill(tmp_path, changed_text):
    """Upgrading an indexed source must correct metadata even when text is unchanged."""
    c = get_connection(tmp_path / 'store.db'); p = tmp_path / 'trace.jsonl'
    before = [rec('Real opening ask', 'ask', 'user'),
              rec(SKILL_BODY, 'skill', 'user'), rec(SUMMARY, 'sum', 'user'),
              rec('Real answer', 'answer')]
    p.write_text(''.join(json.dumps(r) + '\n' for r in before))
    memory.index_file(c, p, session_id='h', cwd=str(tmp_path)); c.commit()
    block = c.execute("SELECT id FROM memory_blocks WHERE message_key='sum'").fetchone()[0]
    after = [before[0], rec(SKILL_BODY, 'skill', 'user', isMeta=True),
             rec(SUMMARY + (' Updated.' if changed_text else ''), 'sum', 'user', isCompactSummary=True), before[3]]
    p.write_text(''.join(json.dumps(r) + '\n' for r in after))
    result = memory.index_file(c, p, session_id='h', cwd=str(tmp_path), rebuild=True); c.commit()
    assert result['state'] == 'complete'
    assert c.execute("SELECT role FROM memory_blocks WHERE id=?", (block,)).fetchone()[0] == 'host'
    assert not c.execute("SELECT 1 FROM memory_blocks WHERE message_key='skill'").fetchone()
    assert any(h['role'] == 'host' for h in memory.search(c, 'continued from a previous conversation'))
    assert block not in [r['id'] for r in memory.brief(c, source_key='claude:h')['evidence']]
    c.close()
