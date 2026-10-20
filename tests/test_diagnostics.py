"""Privacy, resource and failure-isolation contracts for optional diagnostics."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import recall_diagnostics as diag
from recall_mcp import Protocol, RecallService
from prepare_claude_app import prepare
from test_recall_mcp import corpus
from test_claude_app import launch


def test_off_creates_nothing_and_ignores_content(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    diag.Diagnostics().record('recall_search', 'ok', 1, query='private query', response='private answer')
    assert list(tmp_path.iterdir()) == []


def test_numeric_allowlist_never_retains_queries_paths_ids_or_errors(tmp_path):
    path = tmp_path/'events.jsonl'
    log = diag.Diagnostics(path)
    log.record('recall_search', 'ok', 1.234, result_count=2, response_chars=321,
               query='SECRET', text='SECRET', path='/private/SECRET', source_key='SECRET', error='SECRET', block_id='SECRET')
    log.record('SECRET', 'ok', 1)
    log.record('capture', 'SECRET', 1)
    log.record('capture', 'ok', float('nan'))
    log.record('capture', 'ok', 1, error_count=True, passes='SECRET')
    text = path.read_text()
    assert 'SECRET' not in text and '/private' not in text
    rows = [json.loads(x) for x in text.splitlines()[1:]]
    assert len(rows) == 2 and 'error_count' not in rows[1] and 'passes' not in rows[1]
    assert path.stat().st_mode & 0o777 == 0o600


def test_rotation_bounds_storage_and_summarizes_retained_sample(tmp_path, monkeypatch):
    monkeypatch.setattr(diag, 'MAX_BYTES', 600)
    path = tmp_path/'events.jsonl'
    log = diag.Diagnostics(path)
    for i in range(100):
        log.record('recall_search', 'empty' if i % 2 else 'ok', i, response_chars=100)
    assert path.stat().st_size <= 600 and Path(str(path)+'.1').stat().st_size <= 600
    summary = diag.summarize(path)
    assert 1 < summary['events'] < 100
    assert summary['operations']['recall_search']['max_ms'] == 99
    assert summary['ignored_malformed_lines'] == 0


def test_busy_log_drops_event_without_waiting(tmp_path):
    if diag.fcntl is None: pytest.skip('POSIX lock contract')
    path = tmp_path/'events.jsonl'
    fd = os.open(str(path)+'.lock', os.O_RDWR | os.O_CREAT, 0o600)
    try:
        diag.fcntl.flock(fd, diag.fcntl.LOCK_EX | diag.fcntl.LOCK_NB)
        log = diag.Diagnostics(path)
        start = time.monotonic()
        log.record('recall_get', 'ok', 1)
        assert time.monotonic()-start < 0.5
        assert log.dropped == 1 and not path.exists()
    finally:
        os.close(fd)


@pytest.mark.parametrize('kind', ['unrelated', 'symlink', 'hardlink', 'public'])
def test_log_cannot_overwrite_or_follow_unrelated_files(tmp_path, kind):
    protected = tmp_path/'protected'
    protected.write_text('private existing file')
    protected.chmod(0o600)
    path = tmp_path/'events.jsonl'
    if kind == 'symlink': path.symlink_to(protected)
    elif kind == 'hardlink': os.link(protected, path)
    else:
        path.write_text('private existing file')
        path.chmod(0o644 if kind == 'public' else 0o600)
    log = diag.Diagnostics(path)
    log.record('recall_get', 'ok', 1)
    assert log.dropped == 1
    assert protected.read_text() == 'private existing file'
    assert path.read_text() == 'private existing file'


def test_concurrent_writers_never_interleave_events(tmp_path):
    path = tmp_path/'events.jsonl'
    scripts = str(Path(__file__).resolve().parents[1]/'scripts')
    code = 'import sys;sys.path.insert(0,sys.argv[1]);from recall_diagnostics import Diagnostics;d=Diagnostics(sys.argv[2]);[d.record("capture","ok",1,passes=1) for _ in range(50)]'
    children = [subprocess.Popen([sys.executable, '-c', code, scripts, str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(4)]
    for child in children:
        child.communicate(timeout=10)
        assert child.returncode == 0
    summary = diag.summarize(path)
    assert 0 < summary['events'] <= 200 and summary['ignored_malformed_lines'] == 0


def test_mcp_diagnostics_distinguish_errors_and_preserve_store(corpus, tmp_path):
    db, ids = corpus
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    path = tmp_path/'events.jsonl'
    protocol = Protocol(RecallService(db, 'repo-a'), diag.Diagnostics(path))
    protocol.ready = True
    def call(name, args):
        return protocol.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': name, 'arguments': args}})
    assert not call('recall_search', {'query': 'private-query-no-result-98765'})['result']['isError']
    assert call('recall_get', {'block_id': ids['hidden']})['result']['isError']
    original = protocol.service.call
    for message in ('database is locked', 'interrupted', 'missing table PRIVATE'):
        def fail(*args): raise sqlite3.OperationalError(message)
        protocol.service.call = fail
        assert call('recall_status', {})['result']['isError']
    protocol.service.call = original
    rows = [json.loads(x) for x in path.read_text().splitlines()[1:]]
    assert [r['outcome'] for r in rows] == ['empty', 'invalid_request', 'store_busy', 'query_budget', 'store_unavailable']
    assert 'PRIVATE' not in path.read_text() and 'private-query' not in path.read_text() and ids['hidden'] not in path.read_text()
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before


def test_packaged_diagnostics_are_explicit_and_failure_does_not_break_reader(corpus, tmp_path):
    db, ids = corpus
    out = tmp_path/'app'
    path = tmp_path/'events.jsonl'
    receipt = prepare(out, db, 'repo-a', sys.executable, diagnostics=path)
    assert receipt['diagnostics'] == str(path) and not path.exists()
    server = json.loads((out/'desktop-config-snippet.json').read_text())['mcpServers']['recall-reader']
    launch(server, ids, tmp_path)
    assert diag.summarize(path)['events'] == 5
    server['args'][-1] = str(tmp_path/'missing-parent'/'events.jsonl')
    launch(server, ids, tmp_path)
    assert not (tmp_path/'missing-parent').exists()
