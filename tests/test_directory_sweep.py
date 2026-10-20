"""P72 (Fable, app activation): a project directory sweep must not let subagent
transcripts or journals masquerade as the parent session. Found while importing two
scopes' history into the live store: 49 and 991 per-session files carried the parent's
sessionId, were matched to the parent's source, and marked it source_changed."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts')]
import memory_store as memory
from db import get_connection
from recall_memory import parser, run


def rec(text, key, sid, role='assistant', cwd='/p'):
    return json.dumps({'type': role, 'uuid': key, 'sessionId': sid, 'cwd': cwd, 'timestamp': '2026-09-06T00:00:00Z',
                       'message': {'role': role, 'content': [{'type': 'text', 'text': text}]}}) + '\n'


@pytest.fixture
def project(tmp_path):
    """Layout of ~/.claude/projects/<dir>/: main transcripts at the top, per-session subdirs below."""
    main = tmp_path / 'abc.jsonl'
    main.write_text(rec('main opening', 'm1', 'abc', 'user') + rec('main answer', 'm2', 'abc'))
    sub = tmp_path / 'abc' / 'subagents'; sub.mkdir(parents=True)
    (sub / 'agent-1.jsonl').write_text(rec('subagent words', 's1', 'abc'))      # parent's sessionId inside
    wf = tmp_path / 'abc' / 'subagents' / 'workflows' / 'wf_1'; wf.mkdir(parents=True)
    (wf / 'journal.jsonl').write_text(json.dumps({'kind': 'journal', 'step': 1, 'sessionId': 'abc'}) + '\n')
    return tmp_path, main


def test_default_sweep_indexes_only_main_transcripts(project, tmp_path):
    root, main = project
    c = get_connection(tmp_path / 'store.db')
    out = run(parser().parse_args(['index', str(root), '--agent', 'claude', '--cwd', '/p']), c); c.commit()
    assert out['files_discovered'] == 1 and out['conflicts'] == []
    assert [r[0] for r in c.execute('SELECT source_key FROM memory_sources')] == ['claude:abc']
    assert c.execute("SELECT state FROM memory_sources").fetchone()[0] == 'complete'
    c.close()


def test_recursive_sweep_keys_subagents_separately_and_skips_journals(project, tmp_path):
    root, main = project
    c = get_connection(tmp_path / 'store.db')
    out = run(parser().parse_args(['index', str(root), '--agent', 'claude', '--cwd', '/p', '--recursive']), c); c.commit()
    keys = sorted(r[0] for r in c.execute('SELECT source_key FROM memory_sources'))
    assert keys == ['claude:abc', 'claude:abc/agent-1'], keys
    assert out['files_discovered'] == 2            # the journal was recognised as non-conversation and skipped
    assert {r[0] for r in c.execute("SELECT state FROM memory_sources")} == {'complete'}
    assert memory.search(c, 'subagent words', repo_id=None)
    c.close()


def test_another_file_claiming_a_registered_source_is_refused_not_reinterpreted(project, tmp_path):
    root, main = project
    c = get_connection(tmp_path / 'store.db')
    memory.index_file(c, main, session_id='abc', cwd='/p'); c.commit()
    before = dict(c.execute('SELECT state, byte_offset, source_size, path FROM memory_sources').fetchone())
    imposter = tmp_path / 'copy.jsonl'; imposter.write_text(rec('shorter file, same session id', 'x1', 'abc'))
    result = memory.index_file(c, imposter, agent='claude'); c.commit()
    assert result['state'] == 'path_conflict' and result['registered_path'] == str(main.resolve())
    assert dict(c.execute('SELECT state, byte_offset, source_size, path FROM memory_sources').fetchone()) == before
    c.close()


def test_looks_like_transcript(tmp_path):
    t = tmp_path / 't.jsonl'; t.write_text(rec('x', 'a', 's'))
    j = tmp_path / 'j.jsonl'; j.write_text('{"kind":"journal"}\n' * 3)
    e = tmp_path / 'e.jsonl'; e.write_text('')
    assert memory.looks_like_transcript(t) and not memory.looks_like_transcript(j) and not memory.looks_like_transcript(e)
    cx = tmp_path / 'c.jsonl'; cx.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': 'c', 'cwd': '/p'}}) + '\n')
    assert memory.looks_like_transcript(cx, agent='codex') and not memory.looks_like_transcript(j, agent='codex')


@pytest.mark.parametrize('nested', [False, True])
def test_codex_sweep_keeps_native_date_layout(tmp_path, nested):
    root = tmp_path/'sessions'; root.mkdir()
    parent = root/'2026'/'09'/'06' if nested else root
    parent.mkdir(parents=True, exist_ok=True)
    (parent/'rollout.jsonl').write_text(json.dumps({'type':'session_meta', 'payload':{'id':'cx-main','cwd':'/project'}})+'\n'+
        json.dumps({'type':'response_item','payload':{'id':'m','type':'message','role':'user',
            'content':[{'type':'input_text','text':'Codex original evidence'}]}})+'\n')
    c = get_connection(tmp_path/'store.db')
    result = run(parser().parse_args(['index', str(root), '--agent', 'codex']), c); c.commit()
    assert result['files_processed'] == 1
    assert memory.search(c, 'original evidence')[0]['agent'] == 'codex'
    c.close()


def test_independent_capture_uses_same_claude_sweep_policy(project, tmp_path):
    from recall_capture import CaptureWorker
    root, main = project
    c = get_connection(tmp_path/'store.db')
    w = CaptureWorker(c, [root], 'claude')
    result = w.refresh(2)
    assert result['passes'] == 1
    assert [r[0] for r in c.execute('SELECT source_key FROM memory_sources')] == ['claude:abc']
    c.close()


def test_recursive_independent_capture_separates_agents_and_skips_journal(project, tmp_path):
    from recall_capture import CaptureWorker
    root, _ = project
    c = get_connection(tmp_path/'recursive.db')
    w = CaptureWorker(c, [root], 'claude', recursive=True)
    assert w.refresh(2)['passes'] == 2
    assert sorted(r[0] for r in c.execute('SELECT source_key FROM memory_sources')) == ['claude:abc','claude:abc/agent-1']
    assert w.refresh(2)['passes'] == 0
    c.close()
