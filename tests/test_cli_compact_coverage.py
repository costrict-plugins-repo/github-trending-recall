"""P78 (Fable review of P75/P77): the Claude Code CLI path carried a per-source listing
with every search/brief (15 KB on a 22-source scope) after the MCP path went compact.
Both paths now share memory_store.compact_coverage; --full-coverage restores the listing.
A missing store is reported with a code, like the other access failures."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts')]
import memory_store as memory
from db import get_connection
from recall_memory import parser, run


def rec(text, key, sid='s'):
    return json.dumps({'type': 'assistant', 'uuid': key, 'sessionId': sid, 'timestamp': '2026-09-06T00:00:00Z',
                       'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': text}]}}) + '\n'


@pytest.fixture
def store(tmp_path):
    c = get_connection(tmp_path / 'store.db')
    for i in range(3):
        p = tmp_path / f't{i}.jsonl'; p.write_text(rec(f'decision number {i}', f'k{i}', f's{i}'))
        memory.index_file(c, p, session_id=f's{i}', cwd=str(tmp_path))
    c.commit()
    yield c, tmp_path
    c.close()


def test_search_and_brief_return_compact_counts_by_default(store):
    c, tmp = store
    for argv in (['search', 'decision', '--cwd', str(tmp)], ['brief', '--cwd', str(tmp)]):
        cov = run(parser().parse_args(argv), c)['coverage']
        assert 'sources' not in cov
        assert cov['source_count'] == 3 and cov['checked_sources'] == 3 and cov['checked_source_states'] == {'complete': 3}
        assert cov['checked_backlog_bytes'] == 0 and 'status' in cov['details']
    full = run(parser().parse_args(['search', 'decision', '--cwd', str(tmp), '--full-coverage']), c)['coverage']
    assert len(full['sources']) == 3 and 'path' in full['sources'][0]
    assert len(json.dumps(run(parser().parse_args(['search', 'decision', '--cwd', str(tmp)]), c)['coverage'])) < len(json.dumps(full)) / 2


def test_mcp_and_cli_compact_coverage_share_one_shape(store):
    c, tmp = store
    import recall_mcp
    repo = memory.repository_identity(str(tmp))
    mcp = recall_mcp.RecallService(tmp / 'store.db', repo)
    with recall_mcp.read_connection(tmp / 'store.db') as rc:
        mcp_cov = mcp.coverage(rc, compact=True)
    cli_cov = run(parser().parse_args(['search', 'decision', '--cwd', str(tmp)]), c)['coverage']
    assert set(mcp_cov) == set(cli_cov)
    assert {k: v for k, v in mcp_cov.items() if k != 'details'} == {k: v for k, v in cli_cov.items() if k != 'details'}


def test_scoped_cli_counts_exclude_other_repositories_and_sources(store):
    c, tmp = store
    foreign = tmp / 'foreign'; foreign.mkdir()
    path = foreign / 'other.jsonl'; path.write_text(rec('unrelated evidence', 'other', 'foreign'))
    memory.index_file(c, path, session_id='foreign', cwd=str(foreign))
    c.commit()
    for full in ([], ['--full-coverage']):
        for command in (['search', 'decision'], ['brief']):
            args = command + ['--cwd', str(tmp)] + full
            cov = run(parser().parse_args(args), c)['coverage']
            assert cov['semantic']['chunks'] == 3 and cov['source_count'] == 3
            cov = run(parser().parse_args(args + ['--source', 'claude:s1']), c)['coverage']
            assert cov['semantic']['chunks'] == 1 and cov['source_count'] == 1


def test_missing_store_read_has_a_code(tmp_path):
    import subprocess
    p = subprocess.run([sys.executable, str(ROOT / 'scripts' / 'recall_memory.py'), '--db', str(tmp_path / 'absent.db'), 'status'],
                       capture_output=True, text=True, timeout=60)
    body = json.loads(p.stdout or p.stderr)
    assert p.returncode == 1 and body['code'] == 'store_missing' and not (tmp_path / 'absent.db').exists()


def test_missing_search_dependency_is_not_reported_as_missing_store(store, monkeypatch, capsys):
    import recall_memory
    _, tmp = store
    def missing_dependency(*args):
        raise FileNotFoundError('Optional model file disappeared')
    monkeypatch.setattr(recall_memory, 'run', missing_dependency)
    assert recall_memory.main(['--db', str(tmp / 'store.db'), 'search', 'decision', '--semantic']) == 1
    error = json.loads(capsys.readouterr().err)
    assert error['error'] == 'Optional model file disappeared'
    assert error.get('code') != 'store_missing'
