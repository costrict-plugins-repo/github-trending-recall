"""Resumable shared-to-private conversion exposed only through owner maintenance.

Requires a reviewed fingerprint and exclusive cooperative leases. Old clients
must be stopped first. Raw transcripts/backups and prior disclosures are outside
this transition; no forensic erasure or OS-user isolation claim is made.
"""
import hashlib
import json
import os
import tempfile
from pathlib import Path

from db import get_connection, get_read_connection
import memory_store as memory
import recall_privacy as policy
from recall_access import Lease, canonical, gate_path
from recall_private_store import SessionStore


def selectors(owner):
    result = {table: ('source_key=?', (owner,)) for table in (
        'memory_sources', 'memory_blocks', 'memory_revisions',
        'memory_rebuild_blocks', 'memory_rebuild_segments')}
    result['memory_chunks'] = ('block_id IN (SELECT id FROM memory_blocks WHERE source_key=?)', (owner,))
    result['memory_vectors'] = ('chunk_id IN (SELECT id FROM memory_chunks WHERE block_id IN (SELECT id FROM memory_blocks WHERE source_key=?))', (owner,))
    if owner.startswith('claude:'):
        sid = owner[7:]
        result.update({table: ('session_id=?', (sid,)) for table in ('sessions', 'exchanges', 'tags', 'highlights', 'invocations')})
        result['connections'] = ('watcher_session=? AND target_session=?', (sid, sid))
    return result


# Foreign-key order, including the indexes that regenerate FTS through triggers.
ORDER = ('sessions', 'memory_sources', 'exchanges', 'tags', 'highlights', 'connections',
         'invocations', 'memory_blocks', 'memory_chunks', 'memory_vectors',
         'memory_revisions', 'memory_rebuild_blocks', 'memory_rebuild_segments')


def manifest(conn, owner):
    tables = {}
    for table, (where, args) in selectors(owner).items():
        digest, count = hashlib.sha256(), 0
        for row in conn.execute(f'SELECT * FROM {table} WHERE {where} ORDER BY rowid', args):
            digest.update(repr(tuple(row)).encode()); digest.update(b'\n'); count += 1
        tables[table] = {'rows': count, 'sha256': digest.hexdigest()}
    return tables


def fingerprint(tables):
    return hashlib.sha256(json.dumps(tables, sort_keys=True).encode()).hexdigest()


def settings_manifest(conn):
    semantic = None
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='memory_semantic_config'").fetchone():
        row = conn.execute('SELECT * FROM memory_semantic_config').fetchone()
        if row:
            fields = {'id', 'path', 'model', 'format', 'dimension'}
            if not set(row.keys()) <= fields:
                raise ValueError('Unsupported semantic configuration shape')
            semantic = {field: dict(row).get(field) for field in fields}
    return {'keep_last': conn.execute('SELECT keep_last FROM memory_revision_policy WHERE id=1').fetchone()[0],
            'semantic': semantic}


def review_manifest(conn, owner):
    cross = []
    if owner.startswith('claude:'):
        sid = owner[7:]
        cross = [tuple(row) for row in conn.execute('SELECT * FROM connections WHERE (watcher_session=? OR target_session=?) AND watcher_session<>target_session ORDER BY id', (sid, sid))]
    return {'tables': manifest(conn, owner), 'settings': settings_manifest(conn),
            'cross_session_links_removed': len(cross),
            'cross_session_links_hash': hashlib.sha256(repr(cross).encode()).hexdigest(),
            'descendant_sources_preserved': [row[0] for row in conn.execute(
                'SELECT source_key FROM memory_sources WHERE substr(source_key,1,length(?))=? ORDER BY source_key', (owner + '/', owner + '/'))]}


def _validate_owner(owner):
    policy.validate_key(owner)
    if owner.split(':', 1)[0] not in ('claude', 'codex') or '/' in owner.split(':', 1)[1]:
        raise ValueError('Conversion requires an exact root Claude or Codex source')


def preview(shared_path, owner, cwd):
    _validate_owner(owner)
    conn = get_read_connection(shared_path)
    try:
        source = conn.execute('SELECT repo_id FROM memory_sources WHERE source_key=?', (owner,)).fetchone()
        if not source or source[0] != memory.repository_identity(cwd):
            raise ValueError('Selected source is missing or belongs to another repository')
        review = review_manifest(conn, owner)
        return {'owner': owner, 'repo_id': source[0], 'fingerprint': fingerprint(review), **review,
                'limits': 'Original transcripts, backups, exports, prior model context and copies in other sessions remain outside this operation.'}
    finally:
        conn.close()


def atomic_record(path, record):
    raw = (json.dumps(record, sort_keys=True) + '\n').encode()
    if len(raw) > 128 * 1024:
        raise ValueError('Transition journal exceeds its byte budget; no transition written')
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(tmp, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)


def load_record(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        policy._safe_file(fd, 'Privacy transition journal')
        with os.fdopen(fd, 'rb', closefd=False) as stream: raw = stream.read(128 * 1024 + 1)
        if len(raw) > 128 * 1024: raise ValueError('Transition journal exceeds its byte budget')
        data = json.loads(raw, object_pairs_hook=policy._unique_fields)
        if not isinstance(data, dict) or data.get('version') != 1 or data.get('state') not in ('prepared', 'copied', 'purged'):
            raise ValueError('Invalid transition journal; access remains closed')
        _validate_owner(data['owner'])
        review = {key: data[key] for key in ('tables', 'settings', 'cross_session_links_removed', 'cross_session_links_hash', 'descendant_sources_preserved')}
        if data['fingerprint'] != fingerprint(review):
            raise ValueError('Invalid transition manifest; access remains closed')
        return data
    finally:
        os.close(fd)


def _copy(source, target, owner):
    selected = selectors(owner)
    for table in ORDER:
        if table not in selected: continue
        where, args = selected[table]
        columns = [r[1] for r in source.execute(f'PRAGMA table_info({table})')]
        names = ','.join('"' + name + '"' for name in columns)
        sql = f'INSERT INTO {table} ({names}) VALUES (' + ','.join('?' for _ in columns) + ')'
        target.executemany(sql, source.execute(f'SELECT {names} FROM {table} WHERE {where} ORDER BY rowid', args))
    # Legacy FTS has no insert triggers. The durable FTS was built by chunk inserts.
    target.execute("INSERT INTO exchanges_fts(exchanges_fts) VALUES('rebuild')")
    keep = source.execute('SELECT keep_last FROM memory_revision_policy WHERE id=1').fetchone()[0]
    target.execute('UPDATE memory_revision_policy SET keep_last=? WHERE id=1', (keep,))
    # Preserve optional vector configuration; its rows are configuration, not a
    # corpus of other owners. The vectors themselves are selected above.
    if source.execute("SELECT 1 FROM sqlite_master WHERE name='memory_semantic_config'").fetchone():
        target.execute('CREATE TABLE IF NOT EXISTS memory_semantic_config (id INTEGER PRIMARY KEY CHECK(id=1),path TEXT,model TEXT,format TEXT,dimension INTEGER)')
        row = source.execute('SELECT * FROM memory_semantic_config').fetchone()
        if row:
            if not set(row.keys()) <= {'id', 'path', 'model', 'format', 'dimension'}:
                raise ValueError('Unsupported semantic configuration shape')
            target.execute('DELETE FROM memory_semantic_config')
            names = ','.join('"' + k + '"' for k in row.keys())
            target.execute('INSERT INTO memory_semantic_config (' + names + ') VALUES (' + ','.join('?' for _ in row) + ')', tuple(row))


def convert(shared_path, directory, owner, cwd, expected_fingerprint, *, fault=lambda phase: None):
    """Reviewed internal owner action. Returns a backend, not a native activation."""
    plan = preview(shared_path, owner, cwd)
    if plan['fingerprint'] != expected_fingerprint:
        raise ValueError('Source changed since the reviewed preview; review it again')
    shared_path, directory = canonical(shared_path), canonical(directory)
    with Lease(shared_path, exclusive=True) as lease:
        source = get_connection(shared_path, maintenance_lease=lease)
        try:
            source.execute('PRAGMA synchronous=FULL')
            if fingerprint(review_manifest(source, owner)) != expected_fingerprint:
                raise ValueError('Source changed before the maintenance lock; review it again')
            target = directory / (memory.digest(owner) + '.db')
            if os.path.lexists(target): raise ValueError('Private destination already exists; use recovery for an interrupted transition')
            record = dict(plan, version=1, state='prepared', shared=str(shared_path), target=str(target), cwd=str(canonical(cwd)))
            atomic_record(gate_path(shared_path), record)
            fault('prepared')
            return _finish(source, lease, record, fault)
        finally: source.close()


def recover(shared_path, *, fault=lambda phase: None):
    """Resume only the recorded transition; never infer a new owner or target."""
    shared_path = canonical(shared_path)
    with Lease(shared_path, exclusive=True, recovery=True) as lease:
        record = load_record(gate_path(shared_path))
        if record['shared'] != str(shared_path) or memory.repository_identity(record['cwd']) != record['repo_id']:
            raise ValueError('Transition store/repository binding mismatch')
        if canonical(record['target']) == shared_path:
            raise ValueError('Private destination equals shared store')
        source = get_connection(shared_path, maintenance_lease=lease)
        try:
            source.execute('PRAGMA synchronous=FULL')
            return _finish(source, lease, record, fault)
        finally: source.close()


def _finish(source, shared_lease, record, fault):
    owner, target_path = record['owner'], Path(record['target'])
    if target_path.name != memory.digest(owner) + '.db' or target_path.is_symlink():
        raise ValueError('Private destination binding is invalid')
    if owner not in policy.private_sources(source):
        policy.set_mode(source, owner, 'off')
    if not target_path.exists():
        if record['state'] != 'prepared': raise ValueError('Verified private copy is missing; access remains closed')
        SessionStore._create_file(record['shared'], target_path.parent, owner, record['cwd'])
    with Lease(target_path, create=record['state'] == 'prepared', exclusive=True) as private_lease:
        target = get_connection(target_path, private_owner=owner, maintenance_lease=private_lease)
        try:
            target.execute('PRAGMA synchronous=FULL')
            if target.execute('SELECT repo_id FROM recall_private_owner').fetchone()[0] != record['repo_id']:
                raise ValueError('Private repository binding differs from the reviewed source')
            found = manifest(target, owner)
            if found != record['tables']:
                if record['state'] != 'prepared' or any(t['rows'] for t in found.values()):
                    raise ValueError('Private copy differs from the reviewed source; access remains closed')
                if fingerprint(review_manifest(source, owner)) != record['fingerprint']:
                    raise ValueError('Shared source changed during interrupted conversion; access remains closed')
                target.execute('BEGIN IMMEDIATE')
                _copy(source, target, owner)
                if manifest(target, owner) != record['tables']:
                    raise ValueError('Private copy verification failed')
                problems = memory.verify_schema(target)
                if problems: raise ValueError('Private schema verification failed: ' + '; '.join(problems))
                target.commit()
            if settings_manifest(target) != record['settings']:
                raise ValueError('Private revision/vector settings differ from the reviewed source')
            record['state'] = 'copied'
            atomic_record(gate_path(record['shared']), record)
            fault('copied')
            # Private routing is monotonic until a separate reviewed sharing
            # transition exists. A stale shared restore is filtered/refused.
            with policy.journal_lock(source):
                source.execute('BEGIN IMMEDIATE')
                policy.write_journal(source, policy.journal_state(source), private={owner})
                source.commit()
            fault('routed')
            source.execute('BEGIN IMMEDIATE')
            policy.purge_source(source, owner)
            source.commit()
            record['state'] = 'purged'
            atomic_record(gate_path(record['shared']), record)
            fault('purged')
            # FTS segment merges can retain obsolete postings after logical
            # deletion. Rebuild from the remaining content before vacuuming.
            source.execute("INSERT INTO memory_fts(memory_fts) VALUES('rebuild')")
            source.execute("INSERT INTO exchanges_fts(exchanges_fts) VALUES('rebuild')")
            source.commit()
            # Rebuild the shared file without deleted free pages and truncate
            # its WAL. This does not erase backups or guarantee forensic erasure.
            if source.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0]:
                raise ValueError('Shared WAL remains busy; recovery must finish cleanup')
            source.execute('VACUUM')
            if source.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0]:
                raise ValueError('Shared WAL remains busy after cleanup')
            if any(t['rows'] for t in manifest(source, owner).values()):
                raise ValueError('Shared source cleanup is incomplete')
            if manifest(target, owner) != record['tables'] or target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('Final private copy verification failed')
            policy.check_shared_visibility(source)
            fault('verified')
            receipt = Path(str(canonical(record['shared'])) + '.privacy-transition.' + memory.digest(owner) + '.completed.json')
            atomic_record(receipt, record)
            gate_path(record['shared']).unlink()
            sync_directory(canonical(record['shared']).parent)
            return SessionStore(target_path, owner, record['repo_id'], record['shared'])
        finally: target.close()
