"""Owner maintenance for private memory; never exposed as model tools.

External revocation survives content restore. Cooperative locks drain updated
clients. Same-user raw filesystem access and prior disclosures remain outside
this boundary. Selected sharing creates a new snapshot, never a routing grant.
"""
import hashlib
import json
import os
import secrets
from contextlib import contextmanager
from pathlib import Path

from db import get_connection, get_read_connection
import memory_store as memory
import memory_transfer
import recall_privacy as policy
from recall_access import Lease, StoreAccessError, canonical

MAX_CONTROL = 4096
MAX_DISCLOSURE = 1024 * 1024


def control_path(path):
    return Path(str(canonical(path)) + '.private-control.json')


def initialize(path, owner):
    """Called only while constructing a new empty private store."""
    from recall_conversion import atomic_record
    dest = control_path(path)
    if os.path.lexists(dest):
        raise ValueError('Private control already exists; owner recovery is required')
    state = {'version': 1, 'owner': owner, 'epoch': secrets.token_hex(32),
             'access': 'active', 'capture': 'on'}
    atomic_record(dest, state)


def read(path, owner):
    fd = os.open(control_path(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        policy._safe_file(fd, 'Private control')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(MAX_CONTROL + 1)
        if len(raw) > MAX_CONTROL:
            raise ValueError('Private control exceeds its byte budget')
        data = json.loads(raw, object_pairs_hook=policy._unique_fields)
        if (not isinstance(data, dict) or set(data) != {'version', 'owner', 'epoch', 'access', 'capture'}
                or type(data['version']) is not int or data['version'] != 1 or data['owner'] != owner
                or data['access'] not in ('active', 'revoked', 'deleted')
                or data['capture'] not in ('on', 'off')
                or not isinstance(data['epoch'], str) or len(data['epoch']) != 64
                or any(c not in '0123456789abcdef' for c in data['epoch'])):
            raise ValueError('Invalid private control; access refused')
        return data
    finally:
        os.close(fd)


def authorize(path, owner, epoch):
    state = read(path, owner)
    if not isinstance(epoch,str) or state['access'] != 'active' or not secrets.compare_digest(state['epoch'], epoch):
        raise StoreAccessError('Private connection revoked; start a newly authorized owner connection')
    return state


@contextmanager
def maintenance(store):
    with Lease(store.path, exclusive=True) as lease:
        conn = get_connection(store.path, private_owner=store.owner, maintenance_lease=lease)
        try:
            if conn.execute('SELECT repo_id FROM recall_private_owner').fetchone()[0] != store.repo_id:
                raise ValueError('Private repository binding mismatch')
            yield conn, read(store.path, store.owner)
        finally:
            conn.close()


def change(store, action, *, allow_backfill=False):
    """Pause/resume capture or revoke/regrant reader connections. No deletion."""
    from recall_conversion import atomic_record
    if action not in ('pause', 'resume', 'revoke', 'grant'):
        raise ValueError('Unknown private control action')
    with maintenance(store) as (conn, state):
        if state['access'] == 'deleted':
            raise ValueError('Deleted private memory cannot be reactivated; start a new session')
        if action == 'pause':
            state['capture'] = 'off'
        elif action == 'resume':
            if state['access'] != 'active':
                raise ValueError('Grant a new reader connection before resuming capture')
            if state['capture'] == 'off' and not allow_backfill:
                raise ValueError('Resuming can capture the disabled interval; explicitly allow backfill')
            state['capture'] = 'on'
        elif action == 'revoke':
            state.update(access='revoked', capture='off', epoch=secrets.token_hex(32))
        elif action == 'grant':
            # Regrant deliberately does not resume capture; old connections stay revoked.
            state.update(access='active', epoch=secrets.token_hex(32))
        atomic_record(control_path(store.path), state)
        return {k: v for k, v in state.items() if k != 'epoch'}


def deletion_preview(store):
    from recall_conversion import manifest, fingerprint
    conn = get_read_connection(store.path, private_owner=store.owner)
    try:
        state = read(store.path, store.owner)
        tables = manifest(conn, store.owner)
        reviewed = {'owner': store.owner, 'tables': tables, 'epoch': state['epoch']}
        return {'owner': store.owner, 'tables': tables, 'fingerprint': fingerprint(reviewed),
                'limits': 'Original transcripts, backups, shared snapshots and prior disclosures remain. Shared capture stays suppressed.'}
    finally:
        conn.close()


def delete(store, expected_fingerprint, *, fault=lambda phase: None):
    """Revoke before purge; an interrupted deletion remains inaccessible and retryable."""
    from recall_conversion import manifest, fingerprint, atomic_record
    with maintenance(store) as (conn, state):
        tables = manifest(conn, store.owner)
        expected = fingerprint({'owner': store.owner, 'tables': tables, 'epoch': state['epoch']})
        if state['access'] != 'deleted' and expected != expected_fingerprint:
            raise ValueError('Private history changed; review deletion again')
        # A retry of an already deleted store may finish only destructive cleanup.
        state.update(access='deleted', capture='off', epoch=secrets.token_hex(32))
        atomic_record(control_path(store.path), state)
        fault('revoked')
        policy.purge_source(conn, store.owner)
        conn.commit()
        fault('purged')
        conn.execute("INSERT INTO memory_fts(memory_fts) VALUES('rebuild')")
        conn.execute("INSERT INTO exchanges_fts(exchanges_fts) VALUES('rebuild')")
        conn.commit()
        conn.execute('VACUUM')
        if conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0]:
            raise ValueError('Private cleanup remains busy; rerun deletion')
        if any(t['rows'] for t in manifest(conn, store.owner).values()):
            raise ValueError('Private cleanup is incomplete')
        return {'owner': store.owner, 'access': 'deleted', 'capture': 'off',
                'retained': 'Empty owner-tagged database and revocation metadata; no forensic-erasure claim'}


def disclosure_preview(store, selections):
    """Select exact current blocks and ranges; no neighbors or hidden revisions."""
    if not isinstance(selections, list) or not 1 <= len(selections) <= 100:
        raise ValueError('Select 1–100 exact block ranges')
    conn = get_read_connection(store.path, private_owner=store.owner)
    try:
        if read(store.path, store.owner)['access'] == 'deleted':
            raise ValueError('Deleted memory cannot be shared')
        excerpts = []
        size = 0
        for item in selections:
            if not isinstance(item, dict) or set(item) != {'block_id', 'start', 'end'}:
                raise ValueError('Each selection requires block_id, start and end only')
            start, end = item['start'], item['end']
            if type(start) is not int or type(end) is not int or not 0 <= start < end:
                raise ValueError('Invalid character range')
            row = conn.execute('SELECT * FROM memory_blocks WHERE id=? AND source_key=?',
                               (item['block_id'], store.owner)).fetchone()
            if not row or end > len(row['text']):
                raise ValueError('Unknown block or range outside retained text')
            text = row['text'][start:end]
            size += len(text.encode('utf-8'))
            if size > MAX_DISCLOSURE:
                raise ValueError('Disclosure exceeds 1 MiB; select fewer passages')
            excerpts.append({**item, 'content_hash': row['content_hash'], 'text': text,
                             'role': row['role'], 'kind': row['kind'], 'timestamp': row['timestamp']})
        review = {'owner': store.owner, 'repo_id': store.repo_id, 'excerpts': excerpts}
        encoded = json.dumps(review, sort_keys=True, ensure_ascii=False).encode()
        return {**review, 'fingerprint': hashlib.sha256(encoded).hexdigest(),
                'notice': 'Only these excerpts will become a separate shared snapshot. Session routing stays private.'}
    finally:
        conn.close()


def disclose(store, selections, expected_fingerprint):
    """Commit exactly the reviewed snapshot, idempotently, without granting capture."""
    # Read lease spans preview and commit so revocation/deletion cannot race disclosure.
    with Lease(store.path):
        review = disclosure_preview(store, selections)
        if review['fingerprint'] != expected_fingerprint:
            raise ValueError('Private disclosure changed; review the exact excerpts again')
        sid = expected_fingerprint
        key = 'shared-snapshot:' + sid
        data = {'format': 'recall-blocks-v1', 'source': {
            'source_key': key, 'session_id': sid, 'agent': 'shared-snapshot',
            'path': 'recall-shared-snapshot:' + sid, 'project_path': store.repo_id,
            'repo_id': store.repo_id}, 'blocks': []}
        for seq, item in enumerate(review['excerpts']):
            msg = str(seq)
            data['blocks'].append({'id': memory.digest(f"{key}:{msg}:0:{item['kind']}")[:32],
                'source_key': key, 'message_key': msg, 'seq': seq, 'role': item['role'],
                'kind': item['kind'], 'timestamp': item['timestamp'], 'text': item['text'],
                'start_byte': 0, 'end_byte': 0, 'content_hash': memory.digest(item['text']),
                'ordinal': 0, 'generation': 0})
        if not store.shared_path:
            raise ValueError('Shared destination binding is missing')
        shared = get_connection(store.shared_path)
        try:
            if store.owner not in policy.private_sources(shared):
                raise ValueError('Private routing is missing; sharing refused')
            result = memory_transfer.import_source(shared, data)
            shared.execute("UPDATE memory_sources SET state='complete',scope_pinned=1,error=? WHERE source_key=?",
                           ('Reviewed private excerpts; fixed shared snapshot, not a live transcript.', key))
            shared.commit()
            return {**result, 'fingerprint': expected_fingerprint, 'private_routing': 'unchanged'}
        finally:
            shared.close()


def backup(store, destination):
    """Portable owner-tagged content backup; never contains a fresh reader grant."""
    import sqlite3
    destination = Path(destination).expanduser()
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    try:
        source = get_read_connection(store.path, private_owner=store.owner)
        try:
            if read(store.path, store.owner)['access'] == 'deleted':
                raise ValueError('Deleted private memory cannot be backed up')
            target = sqlite3.connect(destination)
            try:
                source.backup(target)
                if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('Private backup integrity check failed')
            finally: target.close()
        finally: source.close()
        with destination.open('rb') as stream: os.fsync(stream.fileno())
        return {'backup':str(destination), 'owner':store.owner, 'bytes':destination.stat().st_size,
                'access_grant_included':False}
    except BaseException:
        destination.unlink()
        raise


@contextmanager
def checked_backup(store, source):
    """Bounded copy from one safe descriptor; never interpret a filename as URI syntax."""
    import tempfile
    from db import SCHEMA_VERSION
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        policy._safe_file(fd, 'Private backup')
        with tempfile.TemporaryDirectory(prefix='recall-private-restore-') as tmp:
            staged = Path(tmp)/'backup.db'
            digest = hashlib.sha256(); size = 0
            with os.fdopen(fd,'rb',closefd=False) as source_stream, staged.open('xb') as out:
                while True:
                    chunk = source_stream.read(1024*1024)
                    if not chunk: break
                    size += len(chunk)
                    if size > 1024**3: raise ValueError('Private backup exceeds 1 GiB restore budget')
                    digest.update(chunk); out.write(chunk)
            staged.chmod(0o600)
            # Check schema before opening any migrator. This release does not
            # silently upgrade or downgrade private content from another release.
            # This is our disposable copy, not the supplied backup or target.
            # A writable staging open lets Apple's SQLite prepare WAL sidecars
            # needed even to read a standalone WAL-mode backup's schema.
            import sqlite3
            raw = sqlite3.connect(staged.as_uri()+'?mode=rw',uri=True)
            try:
                if raw.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION:
                    raise ValueError('Private backup requires its matching schema version')
            finally: raw.close()
            conn = get_connection(staged, private_owner=store.owner)
            try:
                if conn.execute('SELECT repo_id FROM recall_private_owner').fetchone()[0] != store.repo_id:
                    raise ValueError('Private backup repository differs')
                if memory.verify_schema(conn) or conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('Private backup schema or integrity is invalid')
                conn.commit()
                yield conn, digest.hexdigest(), size
            finally: conn.close()
    finally: os.close(fd)


def restore_preview(store, source):
    from recall_conversion import manifest
    with checked_backup(store, source) as (conn, fingerprint, size):
        return {'owner':store.owner,'fingerprint':fingerprint,'bytes':size,
                'tables':manifest(conn,store.owner),
                'notice':'Replaces private content and revokes existing readers. Capture remains off; explicitly grant and resume after review.'}


def restore(store, source, expected_fingerprint):
    from recall_conversion import atomic_record
    with checked_backup(store, source) as (staged, fingerprint, size):
        if fingerprint != expected_fingerprint: raise ValueError('Backup changed; review it again')
        with maintenance(store) as (target, state):
            if state['access'] == 'deleted':
                raise ValueError('Deleted owner cannot be restored or reactivated')
            state.update(access='revoked',capture='off',epoch=secrets.token_hex(32))
            atomic_record(control_path(store.path),state)
            staged.backup(target)
            target.commit()
            if target.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0]:
                raise ValueError('Restored WAL is busy; access remains revoked')
            return {'owner':store.owner,'restored_bytes':size,'access':'revoked','capture':'off'}
