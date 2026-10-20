"""Regressions from real cross-agent coverage and parent-directory usage failures."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
from db import get_connection
import memory_store as memory
from recall_memory import parser, run
from recall_capture import CaptureWorker
import pytest


def trace(path, sid, cwd):
    path.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': sid, 'cwd': str(cwd)}})+'\n'+
        json.dumps({'type': 'response_item', 'payload': {'id': 'one', 'type': 'message', 'role': 'user',
            'content': [{'type': 'input_text', 'text': 'accepted amber protocol'}]}})+'\n')


def test_explicit_cli_mapping_survives_background_capture(tmp_path):
    c = get_connection(tmp_path/'store.db')
    source = tmp_path/'session.jsonl'; repo = tmp_path/'project'; repo.mkdir()
    trace(source, 's', tmp_path)
    result = run(parser().parse_args(['index', str(source), '--agent', 'codex', '--cwd', str(repo)]), c)
    assert result['results'][0]['state'] == 'complete'
    memory.index_file(c, source, agent='codex'); c.commit()
    row = c.execute('SELECT * FROM memory_sources').fetchone()
    assert row['repo_id'] == memory.repository_identity(repo) and row['scope_pinned'] == 1
    assert memory.search(c, 'amber', repo_id=memory.repository_identity(repo))
    assert not memory.search(c, 'amber', repo_id=memory.repository_identity(tmp_path))
    c.close()


def test_explicit_cli_mapping_cannot_move_registered_history(tmp_path):
    c = get_connection(tmp_path/'store.db')
    source = tmp_path/'session.jsonl'; foreign = tmp_path/'foreign'; foreign.mkdir()
    trace(source, 's', tmp_path)
    memory.index_file(c, source, agent='codex'); c.commit()
    before = tuple(c.execute('SELECT * FROM memory_sources').fetchone())
    result = run(parser().parse_args(['index', str(source), '--agent', 'codex', '--cwd', str(foreign)]), c)
    assert result['results'][0]['state'] == 'scope_mismatch'
    assert tuple(c.execute('SELECT * FROM memory_sources').fetchone()) == before
    c.close()


def test_capture_and_cli_share_mapping_contract(tmp_path):
    c = get_connection(tmp_path/'store.db')
    source = tmp_path/'session.jsonl'; repo = tmp_path/'project'; repo.mkdir()
    trace(source, 's', tmp_path)
    result = CaptureWorker(c, [source], 'codex', cwd=repo).refresh(2)
    assert not result['errors']
    result = CaptureWorker(c, [source], 'codex', cwd=tmp_path).refresh(2)
    assert result['errors'][0]['state'] == 'scope_mismatch'
    assert c.execute('SELECT scope_pinned FROM memory_sources').fetchone()[0] == 1
    c.close()


def test_agent_coverage_counts_entire_scope_not_only_latest_page(tmp_path):
    c = get_connection(tmp_path/'store.db')
    repo = memory.repository_identity(tmp_path)
    for i, agent in enumerate(['claude', 'claude', 'codex', 'codex']):
        c.execute('INSERT INTO memory_sources(source_key,session_id,agent,path,project_path,repo_id) VALUES(?,?,?,?,?,?)',
                  (agent+':'+str(i), str(i), agent, str(tmp_path/'absent'), str(tmp_path), repo if i < 3 else 'foreign'))
    scoped = memory.status(c, repo, limit=1)
    assert len(scoped['sources']) == 1 and scoped['source_agents'] == {'claude': 2, 'codex': 1}
    assert memory.compact_coverage(scoped, repo)['source_agents'] == scoped['source_agents']
    assert memory.status(c, source_key='claude:0')['source_agents'] == {'claude': 1}
    assert memory.status(c, 'empty')['source_agents'] == {}
    c.close()


def test_skill_install_never_opens_or_creates_a_store(tmp_path, monkeypatch):
    import recall_memory
    def forbidden(*args, **kwargs):
        raise AssertionError('Skill installation must not open the memory store')
    monkeypatch.setattr(recall_memory, 'get_connection', forbidden)
    monkeypatch.setattr(recall_memory, 'get_read_connection', forbidden)
    db = tmp_path/'uncreated'/'store.db'
    assert recall_memory.main(['--db', str(db), 'install-codex-skill',
                              '--skills-dir', str(tmp_path/'skills')]) == 0
    assert (tmp_path/'skills/recall/SKILL.md').is_file()
    assert not db.parent.exists()


@pytest.mark.parametrize('kind', ['worktree-state', 'relocated', 'atis-latch',
    'bridge-session', 'file-history-delta', 'frame-link', 'cost-state', 'pr-link'])
def test_observed_claude_bookkeeping_is_metadata(kind):
    record = {'type': kind, 'sessionId': 's'}
    assert list(memory.normalize_record(record, 'claude')) == []
    assert memory.classify_skipped(record, 'claude') == 'metadata'
    assert memory.classify_skipped(record, 'codex') == 'unsupported'


def test_fallback_notices_do_not_hide_unknown_or_mixed_prose():
    record = {'type': 'assistant', 'message': {'role': 'assistant', 'content': [
        {'type': 'fallback', 'from': {'model': 'a'}, 'to': {'model': 'b'}}]}}
    assert list(memory.normalize_record(record, 'claude')) == []
    assert memory.classify_skipped(record, 'claude') == 'metadata'
    record['message']['content'].append({'type': 'text', 'text': 'Actual decision'})
    assert list(memory.normalize_record(record, 'claude'))[0][2] == 'Actual decision'
    record['message']['content'] = [{'type': 'future_notice'}]
    assert memory.classify_skipped(record, 'claude') == 'unsupported'
