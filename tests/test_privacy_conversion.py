"""Reviewed offline conversion, copy integrity, crash recovery and access draining."""
import json
import sqlite3
import sys
from pathlib import Path

import pytest
sys.path[:0] = [str(Path(__file__).parents[1] / 'scripts'), str(Path(__file__).parents[1] / 'hooks')]
from db import get_connection, get_read_connection
from prompt_submit import index_transcript
from recall_access import Lease, gate_path
from recall_mcp import RecallService, read_connection
from recall_memory import parser, run
import recall_conversion as conversion
import recall_privacy as policy
import memory_store as memory
import memory_revisions as revisions
from test_capture_policy import write


@pytest.fixture(params=['claude', 'codex'])
def corpus(tmp_path, request):
    agent = request.param
    path = tmp_path / 'shared.db'
    c = get_connection(path)
    transcript = write(tmp_path / 's.jsonl', agent)
    if agent == 'claude':
        index_transcript(c, 's', str(transcript))
    else:
        memory.index_file(c, transcript, agent='codex')
    owner = agent + ':s'
    # Retained history/pins and vectors must survive conversion without encoding.
    row = dict(c.execute('SELECT * FROM memory_blocks WHERE source_key=? LIMIT 1', (owner,)).fetchone())
    old = dict(row, text='older secret violet', content_hash=memory.digest('older secret violet'))
    revisions.remember(c, old, pinned=True)
    chunk = c.execute('SELECT id FROM memory_chunks LIMIT 1').fetchone()[0]
    c.execute('INSERT INTO memory_vectors VALUES(?,?,?)', (chunk, 'fixture', b'\x00\x00\x80?'))
    # Same bare ID in the other adapter must remain shared and unchanged.
    other = 'codex' if agent == 'claude' else 'claude'
    memory.index_file(c, write(tmp_path / 'foreign.jsonl', other), agent=other)
    c.commit()
    foreign = conversion.manifest(c, other + ':s')
    backup = tmp_path / 'old.db'
    run(parser().parse_args(['backup', str(backup)]), c)
    c.close()
    return path, owner, tmp_path, foreign, other, backup


def start(corpus, fault=lambda phase: None):
    path, owner, root, *_ = corpus
    plan = conversion.preview(path, owner, root)
    return conversion.convert(path, root / 'private', owner, root, plan['fingerprint'], fault=fault)


def test_complete_conversion_preserves_owner_and_foreign_evidence(corpus):
    path, owner, root, foreign, other, backup = corpus
    plan = conversion.preview(path, owner, root)
    store = start(corpus)
    assert not gate_path(path).exists()
    assert Path(str(path) + '.privacy-transition.' + memory.digest(owner) + '.completed.json').is_file()
    target = get_connection(store.path, private_owner=owner)
    assert conversion.manifest(target, owner) == plan['tables']
    assert not memory.verify_schema(target)
    assert target.execute('SELECT count(*) FROM memory_revisions WHERE pinned=1').fetchone()[0] == 1
    target.close()
    shared = get_connection(path)
    assert conversion.manifest(shared, other + ':s') == foreign
    assert not memory.verify_schema(shared)
    assert not any(t['rows'] for t in conversion.manifest(shared, owner).values())
    assert owner in policy.private_sources(shared)
    with pytest.raises(ValueError, match='private routing'):
        policy.set_mode(shared, owner, 'shared', allow_backfill=True)
    assert memory.index_file(shared, root / 's.jsonl', agent=owner.split(':')[0])['state'] == 'capture_disabled'
    shared.commit()
    # Explicit restore drops the now-private owner's stale shared representations.
    run(parser().parse_args(['restore', str(backup), '--yes']), shared)
    assert not any(t['rows'] for t in conversion.manifest(shared, owner).values())
    assert conversion.manifest(shared, other + ':s') == foreign
    shared.close()
    assert store.reader().call('recall_search', {'query': 'amber'})['hits']


@pytest.mark.parametrize('phase', ['prepared', 'copied', 'routed', 'purged', 'verified'])
def test_interrupted_conversion_closes_access_and_recovers(corpus, phase):
    path, owner, root, *_ = corpus
    def crash(point):
        if point == phase: raise RuntimeError('injected ' + point)
    with pytest.raises(RuntimeError, match='injected'):
        start(corpus, crash)
    assert gate_path(path).exists()
    for open_connection in (get_connection, get_read_connection):
        with pytest.raises(ValueError, match='transition is incomplete'):
            open_connection(path)
    with pytest.raises(ValueError, match='transition is incomplete'):
        RecallService(path, memory.repository_identity(root)).call('recall_status', {})
    store = conversion.recover(path)
    assert store.reader().call('recall_search', {'query': 'amber'})['hits']
    assert not gate_path(path).exists()


def test_changed_preview_refused_before_any_policy_change(corpus):
    path, owner, root, *_ = corpus
    plan = conversion.preview(path, owner, root)
    c = get_connection(path)
    c.execute("UPDATE memory_sources SET error='changed' WHERE source_key=?", (owner,)); c.commit(); c.close()
    with pytest.raises(ValueError, match='changed'):
        conversion.convert(path, root / 'private', owner, root, plan['fingerprint'])
    assert not gate_path(path).exists()
    c = get_connection(path); assert policy.mode(c, owner) == 'shared'; c.close()


@pytest.mark.parametrize('kind', ['writer', 'cli-reader', 'mcp-reader'])
def test_active_connections_prevent_conversion_without_mutation(corpus, kind):
    path, owner, root, *_ = corpus
    if kind == 'mcp-reader':
        with read_connection(path):
            with pytest.raises(ValueError, match='active connections'):
                start(corpus)
    else:
        c = get_connection(path) if kind == 'writer' else get_read_connection(path)
        with pytest.raises(ValueError, match='active connections'):
            start(corpus)
        c.close()
    assert not gate_path(path).exists()
    c = get_connection(path); assert policy.mode(c, owner) == 'shared'; c.close()


def test_raw_stale_restore_is_denied_by_updated_readers(corpus):
    path, owner, root, _, _, backup = corpus
    start(corpus)
    old = sqlite3.connect(backup); target = sqlite3.connect(path)
    old.backup(target); target.close(); old.close()
    for opener in (get_read_connection, get_connection):
        with pytest.raises(ValueError, match='resurfaced'):
            opener(path)


def test_wrong_scope_and_fork_are_refused(corpus):
    path, owner, root, *_ = corpus
    unrelated = root / 'unrelated'; unrelated.mkdir()
    with pytest.raises(ValueError, match='another repository'):
        conversion.preview(path, owner, unrelated)
    with pytest.raises(ValueError, match='root'):
        conversion.preview(path, owner + '/subagent', root)


def test_settings_and_staged_rebuild_are_preserved(corpus):
    path, owner, root, *_ = corpus
    c = get_connection(path)
    c.execute('UPDATE memory_revision_policy SET keep_last=7')
    c.execute('CREATE TABLE memory_semantic_config (id INTEGER PRIMARY KEY,path TEXT,model TEXT,format TEXT,dimension INTEGER)')
    c.execute("INSERT INTO memory_semantic_config VALUES(1,'/offline/model','fixture','f32le-v1',1)")
    row = dict(c.execute('SELECT * FROM memory_blocks WHERE source_key=? LIMIT 1', (owner,)).fetchone())
    row['text'] = 'staged amber revision'; row['content_hash'] = memory.digest(row['text'])
    c.execute('INSERT INTO memory_rebuild_blocks (' + revisions.COLUMNS + ') VALUES (' + ','.join('?' for _ in revisions.FIELDS) + ')', tuple(row[k] for k in revisions.FIELDS))
    c.execute('INSERT INTO memory_rebuild_segments VALUES(?,?,?,?)', (owner, 0, 1, 'fixture-hash'))
    c.commit(); c.close()
    store = start(corpus)
    c = get_connection(store.path, private_owner=owner)
    assert c.execute('SELECT keep_last FROM memory_revision_policy').fetchone()[0] == 7
    assert c.execute('SELECT model,dimension FROM memory_semantic_config').fetchone()[:] == ('fixture', 1)
    assert c.execute('SELECT text FROM memory_rebuild_blocks').fetchone()[0] == 'staged amber revision'
    assert c.execute('SELECT count(*) FROM memory_rebuild_segments').fetchone()[0] == 1
    c.close()


def test_partial_copy_transaction_rolls_back_and_recovers(corpus, monkeypatch):
    path, owner, root, *_ = corpus
    original = conversion._copy
    def fail(source, target, selected):
        original(source, target, selected)
        raise RuntimeError('mid-copy failure')
    monkeypatch.setattr(conversion, '_copy', fail)
    with pytest.raises(RuntimeError, match='mid-copy'):
        start(corpus)
    monkeypatch.setattr(conversion, '_copy', original)
    store = conversion.recover(path)
    assert store.reader().call('recall_search', {'query': 'amber'})['hits']


def test_private_copy_tampering_prevents_shared_deletion(corpus):
    path, owner, root, *_ = corpus
    def stop(phase):
        if phase == 'copied': raise RuntimeError('stop')
    with pytest.raises(RuntimeError): start(corpus, stop)
    record = conversion.load_record(gate_path(path))
    c = get_connection(record['target'], private_owner=owner)
    c.execute("UPDATE memory_blocks SET text='tampered' WHERE source_key=?", (owner,)); c.commit(); c.close()
    with pytest.raises(ValueError, match='differs'):
        conversion.recover(path)
    with Lease(path, exclusive=True, recovery=True) as lease:
        c = get_connection(path, maintenance_lease=lease)
        assert conversion.manifest(c, owner)['memory_blocks']['rows'] > 0
        c.close()


def test_cross_session_links_are_reviewed_and_not_copied(tmp_path):
    path = tmp_path / 'shared.db'; c = get_connection(path)
    for sid in ('s', 'peer'):
        index_transcript(c, sid, str(write(tmp_path / (sid + '.jsonl'), 'claude', sid)))
    c.execute("INSERT INTO tags(tag,session_id,source,created_at) VALUES('decision','s','user','today')")
    c.execute("INSERT INTO highlights(session_id,summary,tags,source,created_at) VALUES('s','owned highlight','[]','user','today')")
    c.commit(); c.close()
    plan = conversion.preview(path, 'claude:s', tmp_path)
    c = get_connection(path)
    c.execute("INSERT INTO connections(watcher_session,target_session,topic,created_at) VALUES('s','peer','shared edge','today')")
    c.commit(); c.close()
    with pytest.raises(ValueError, match='changed'):
        conversion.convert(path, tmp_path / 'private', 'claude:s', tmp_path, plan['fingerprint'])
    plan = conversion.preview(path, 'claude:s', tmp_path)
    assert plan['cross_session_links_removed'] == 1
    store = conversion.convert(path, tmp_path / 'private', 'claude:s', tmp_path, plan['fingerprint'])
    c = get_connection(store.path, private_owner='claude:s')
    assert c.execute('SELECT summary FROM highlights').fetchone()[0] == 'owned highlight'
    assert c.execute('SELECT tag FROM tags').fetchone()[0] == 'decision'
    assert not c.execute('SELECT * FROM connections').fetchall(); c.close()
    c = get_connection(path)
    assert c.execute("SELECT 1 FROM sessions WHERE session_id='peer'").fetchone()
    assert not c.execute('SELECT * FROM connections').fetchall(); c.close()


def test_published_empty_store_without_lease_recovers(corpus, monkeypatch):
    from recall_private_store import SessionStore
    path, owner, root, *_ = corpus
    original = SessionStore._create_file
    def crash(*args):
        result = original(*args)
        Path(str(result.path) + '.access.lock').unlink()
        raise RuntimeError('after empty-file publication')
    monkeypatch.setattr(SessionStore, '_create_file', crash)
    with pytest.raises(RuntimeError): start(corpus)
    monkeypatch.setattr(SessionStore, '_create_file', original)
    store = conversion.recover(path)
    assert store.reader().call('recall_search', {'query': 'amber'})['hits']


def test_copied_shared_store_requires_writer_lease_setup_without_read_writes(tmp_path):
    original = tmp_path / 'original.db'; copy = tmp_path / 'copy.db'
    c = get_connection(original); dest = sqlite3.connect(copy)
    c.backup(dest); dest.close(); c.close()
    before = sorted(p.name for p in tmp_path.iterdir())
    with pytest.raises(ValueError, match='lease is missing'):
        get_read_connection(copy)
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    c = get_connection(copy); c.close()
    c = get_read_connection(copy); c.close()


def test_bad_database_releases_lease_even_while_exception_is_retained(tmp_path):
    path = tmp_path / 'bad.db'; path.write_bytes(b'not a database')
    captured = None
    try:
        get_connection(path)
    except sqlite3.Error as exc:
        captured = exc
    assert captured is not None
    with Lease(path, exclusive=True):
        pass


def test_access_lock_symlink_is_refused(tmp_path):
    path = tmp_path / 'store.db'; c = get_connection(path); c.close()
    lock = Path(str(path) + '.access.lock'); other = tmp_path / 'other.lock'
    lock.rename(other); lock.symlink_to(other)
    for opener in (get_connection, get_read_connection):
        with pytest.raises(OSError): opener(path)


def test_nonposix_shared_use_survives_but_privacy_maintenance_is_refused(tmp_path, monkeypatch):
    import recall_access as access
    monkeypatch.setattr(access, 'fcntl', None)
    monkeypatch.setattr(policy, '_POSIX', False)
    path = tmp_path / 'shared.db'; c = get_connection(path)
    memory.index_file(c, write(tmp_path / 's.jsonl'), agent='codex'); c.commit()
    from memory_transfer import export_source, import_source
    dest = get_connection(tmp_path / 'import.db')
    import_source(dest, export_source(c, 'codex:s')); dest.commit()
    assert memory.search(dest, 'amber'); dest.close()
    with pytest.raises(ValueError, match='POSIX'):
        policy.set_mode(c, 'codex:s', 'off')
    c.close()
    c = get_read_connection(path); assert memory.search(c, 'amber'); c.close()
    with pytest.raises(ValueError, match='POSIX'):
        start((path, 'codex:s', tmp_path))
    assert not gate_path(path).exists()


def test_nonposix_host_cannot_ignore_an_existing_privacy_journal(tmp_path, monkeypatch):
    import recall_access as access
    path = tmp_path / 'shared.db'; c = get_connection(path)
    policy.set_mode(c, 'codex:s', 'off'); c.close()
    monkeypatch.setattr(access, 'fcntl', None)
    monkeypatch.setattr(policy, '_POSIX', False)
    for opener in (get_read_connection, get_connection):
        with pytest.raises(ValueError, match='unavailable'):
            opener(path)


@pytest.mark.parametrize('state,outcome', [('busy', 'store_busy'), ('incomplete', 'store_unavailable')])
def test_mcp_diagnostics_distinguish_maintenance_from_bad_queries(tmp_path, state, outcome):
    from recall_mcp import Protocol
    path = tmp_path / 'store.db'; c = get_connection(path); c.close()
    class Recorder:
        events = []
        def record(self, operation, result, *args, **kwargs): self.events.append(result)
    recorder = Recorder(); protocol = Protocol(RecallService(path, memory.repository_identity(tmp_path)), recorder)
    protocol.ready = True
    call = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'recall_status', 'arguments': {}}}
    if state == 'busy':
        with Lease(path, exclusive=True): response = protocol.handle(call)
    else:
        gate_path(path).write_text('{}')
        response = protocol.handle(call)
    assert response['result']['isError']
    assert recorder.events == [outcome]


def test_private_route_check_covers_more_than_one_sql_parameter_batch(tmp_path):
    path = tmp_path / 'shared.db'; c = get_connection(path)
    memory.index_file(c, write(tmp_path / 's.jsonl', 'codex', 'zz-real'), agent='codex'); c.commit()
    with policy.journal_lock(c):
        c.execute('BEGIN IMMEDIATE')
        policy.write_journal(c, set(), private={'codex:fake-' + str(i) for i in range(1500)} | {'codex:zz-real'})
        c.commit()
    c.close()
    with pytest.raises(ValueError, match='resurfaced'):
        get_read_connection(path)
