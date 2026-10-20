"""Capture policy foundation. Owner administration, not a model identity service.

Shared/off is enforced in SQLite as well as adapters. Private stores and host attachment use the separate owner-maintenance and native
controller modules; this module never grants a model-selected owner identity.
"""
import json
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

MODES = ('shared', 'off')
PRIVATE_APPLICATION_ID = 0x52435056
_POSIX = os.name == 'posix'
TABLE = '''CREATE TABLE IF NOT EXISTS recall_capture_policy (
 source_key TEXT PRIMARY KEY,
 mode TEXT NOT NULL CHECK(mode IN ('shared','off')),
 updated_at TEXT NOT NULL
)'''


def initialize(conn):
    conn.execute(TABLE)
    conn.execute('''CREATE TABLE IF NOT EXISTS recall_policy_journal (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        required INTEGER NOT NULL CHECK(required=1))''')
    # Keep the policy independent of content: prune must not remove suppression.
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, expression in [('memory_sources', 'NEW.source_key'), ('memory_blocks', 'NEW.source_key'),
        ('memory_rebuild_blocks', 'NEW.source_key'), ('memory_revisions', 'NEW.source_key'),
        ('sessions', "'claude:' || NEW.session_id"), ('exchanges', "'claude:' || NEW.session_id"),
        ('tags', "'claude:' || NEW.session_id"), ('highlights', "'claude:' || NEW.session_id")]:
        if table not in tables:
            continue
        for operation in ('INSERT', 'UPDATE'):
            conn.execute(f'''CREATE TRIGGER IF NOT EXISTS recall_policy_{table}_{operation.lower()}
            BEFORE {operation} ON {table}
            WHEN COALESCE((SELECT mode FROM recall_capture_policy WHERE source_key={expression}), 'shared') <> 'shared'
            BEGIN SELECT RAISE(ABORT, 'Recall capture disabled for this source'); END''')


def validate_key(key):
    if not isinstance(key, str) or not re.fullmatch(r'[a-z][a-z0-9-]*:[^\s\x00-\x1f]{1,512}', key):
        raise ValueError('Expected an exact agent:session source identity')
    return key


def mode(conn, key):
    validate_key(key)
    if key in journal_state(conn):
        return 'off'
    row = conn.execute('SELECT mode FROM recall_capture_policy WHERE source_key=?', (key,)).fetchone()
    value = row[0] if row else 'shared'
    if value not in MODES:
        raise ValueError('Unknown capture policy; capture refused')
    return value


def require_capture(conn, key):
    if mode(conn, key) != 'shared':
        raise ValueError('Recall capture disabled for this source')


def set_mode(conn, key, value, allow_backfill=False):
    if not _POSIX:
        raise ValueError('Privacy policy controls require a POSIX host in this candidate')
    validate_key(key)
    if value not in MODES:
        raise ValueError('Supported capture modes are shared/off. Session-only is unavailable until a trusted owner connection is established.')
    with journal_lock(conn):
        if key in private_sources(conn):
            raise ValueError('This source has a private routing decision; use the reviewed privacy transition workflow')
        # Serialize with capture; the external journal is always denied first
        # and granted last. A crash at either commit boundary stays restrictive.
        if not conn.in_transaction:
            conn.execute('BEGIN IMMEDIATE')
        previous = mode(conn, key)
        if previous == 'off' and value == 'shared' and not allow_backfill:
            raise ValueError('Re-enabling shared capture can import history written while disabled; explicitly allow backfill or keep capture off')
        entries = journal_state(conn)
        if value == 'off':
            entries.add(key)
            write_journal(conn, entries)
        conn.execute('INSERT INTO recall_capture_policy VALUES(?,?,?) ON CONFLICT(source_key) DO UPDATE SET mode=excluded.mode,updated_at=excluded.updated_at',
                     (key, value, datetime.now(timezone.utc).isoformat()))
        conn.commit()
        if value == 'shared' and key in entries:
            entries.remove(key)
            write_journal(conn, entries)
            conn.commit()
    return {'source': key, 'mode': value, 'previous': previous,
            'existing_memory': 'unchanged; stopping capture does not erase previously retained or exported history',
            'scope': 'this store and its updated Recall writers; not host transcript retention or filesystem isolation'}


def export_policy(conn, key):
    return {'version': 1, 'mode': mode(conn, key)}


def preserve_restore_policy(current, staged):
    """Current owner decisions take precedence over stale backup decisions."""
    for row in current.execute('SELECT source_key,mode,updated_at FROM recall_capture_policy'):
        staged.execute('INSERT INTO recall_capture_policy VALUES(?,?,?) ON CONFLICT(source_key) DO UPDATE SET mode=excluded.mode,updated_at=excluded.updated_at', tuple(row))
    entries = journal_state(current) | {row[0] for row in staged.execute("SELECT source_key FROM recall_capture_policy WHERE mode='off'")}
    if entries or staged.execute('SELECT 1 FROM recall_policy_journal').fetchone() or current.execute('SELECT 1 FROM recall_policy_journal').fetchone():
        write_journal(current, entries)
        current.commit()
        staged.execute('INSERT OR IGNORE INTO recall_policy_journal VALUES(1,1)')
        for key in entries:
            staged.execute("INSERT INTO recall_capture_policy VALUES(?,'off',?) ON CONFLICT(source_key) DO UPDATE SET mode='off'", (key, datetime.now(timezone.utc).isoformat()))
    for key in private_sources(current):
        purge_source(staged, key)
    staged.commit()


def journal_path(conn):
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    return Path(path + '.capture-policy.json') if path else None


def _safe_file(fd, label='Capture journal'):
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
        raise ValueError(label + ' requires a regular owner-only file; access refused')


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate capture journal field; capture refused')
        result[key] = value
    return result


def journal_state(conn):
    data = journal_data(conn)
    return set(data['off']) | set(data.get('private', []))


def private_sources(conn):
    return set(journal_data(conn).get('private', []))


def journal_data(conn):
    """Read the deny-only ledger on every policy check, including old connections.

    Kept beside, not inside, the restorable content DB. In-memory test stores have
    no persistent history or journal. Read-only retrieval does not call this gate:
    stopping capture does not revoke already retained content.
    """
    path = journal_path(conn)
    if path is None:
        return {'version': 1, 'off': []}
    required = conn.execute('SELECT 1 FROM recall_policy_journal').fetchone()
    if not _POSIX:
        if required or os.path.lexists(path):
            raise ValueError('Privacy policy controls are unavailable on this host; access refused')
        return {'version': 1, 'off': []}
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        if required:
            raise ValueError('Required capture journal is missing; capture refused. Restore the journal before resuming writes.')
        return {'version': 1, 'off': []}
    try:
        _safe_file(fd)
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError('Capture journal exceeds its byte budget; capture refused')
        data = json.loads(raw, object_pairs_hook=_unique_fields)
        if not isinstance(data, dict) or type(data.get('version')) is not int or data['version'] not in (1, 2):
            raise ValueError('Unsupported capture journal; capture refused')
        fields = {'version', 'off'} if data['version'] == 1 else {'version', 'off', 'private'}
        if set(data) != fields:
            raise ValueError('Unsupported capture journal; capture refused')
        for field in fields - {'version'}:
            if not isinstance(data[field], list):
                raise ValueError('Invalid capture journal identities; capture refused')
            keys = {validate_key(key) for key in data[field]}
            if len(keys) != len(data[field]):
                raise ValueError('Duplicate capture journal identity; capture refused')
        return data
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError('Invalid capture journal; capture refused') from exc
    finally:
        os.close(fd)


@contextmanager
def journal_lock(conn):
    """Serialize owner controls/restore; never wait behind an inverted DB lock."""
    if not _POSIX:
        journal_state(conn)  # Refuse any protected store; ordinary import remains usable.
        yield
        return
    import fcntl
    path = journal_path(conn)
    if path is None:
        yield
        return
    fd = os.open(str(path) + '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        _safe_file(fd)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError('Another capture-policy or restore operation is active; retry after it completes') from exc
        yield
    finally:
        os.close(fd)


def write_journal(conn, entries, *, private=None):
    """Caller holds journal_lock and the content write lock; fsync before return."""
    if not _POSIX:
        raise ValueError('Privacy policy controls require a POSIX host in this candidate')
    path = journal_path(conn)
    if path is None:
        return
    old_private = private_sources(conn)  # Also validates the existing journal.
    private = old_private if private is None else old_private | set(private)
    data = {'version': 2 if private else 1, 'off': sorted(set(entries) | private)}
    if private:
        data['private'] = sorted(private)
    raw = (json.dumps(data, separators=(',', ':')) + '\n').encode()
    if len(raw) > 1024 * 1024:
        raise ValueError('Capture journal exceeds its byte budget')
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        conn.execute('INSERT OR IGNORE INTO recall_policy_journal VALUES(1,1)')
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def purge_source(conn, key):
    """Exact owner maintenance primitive; caller holds its transaction/lease."""
    validate_key(key)
    conn.execute('DELETE FROM memory_sources WHERE source_key=?', (key,))
    if key.startswith('claude:') and '/' not in key.split(':', 1)[1]:
        sid = key.split(':', 1)[1]
        from db import _delete_fts_rows
        _delete_fts_rows(conn, sid)
        for table in ('tags', 'highlights', 'exchanges', 'sessions'):
            if table == 'sessions':
                conn.execute('DELETE FROM connections WHERE watcher_session=? OR target_session=?', (sid, sid))
            conn.execute(f'DELETE FROM {table} WHERE session_id=?', (sid,))
        conn.execute('DELETE FROM invocations WHERE session_id=?', (sid,))


def check_shared_visibility(conn):
    # A stale content restore cannot reintroduce a converted private owner into
    # updated shared readers. Normal off history remains readable.
    keys = sorted(private_sources(conn))
    # Bound SQL parameters for older SQLite builds, without issuing a query for
    # every historical private session whenever a normal reader opens.
    for start in range(0, len(keys), 400):
        batch = keys[start:start + 400]
        marks = ','.join('?' for _ in batch)
        found = conn.execute('SELECT 1 FROM memory_sources WHERE source_key IN (' + marks + ') LIMIT 1', batch).fetchone()
        legacy = [key[7:] for key in batch if key.startswith('claude:')]
        if not found and legacy:
            found = conn.execute('SELECT 1 FROM sessions WHERE session_id IN (' + ','.join('?' for _ in legacy) + ') LIMIT 1', legacy).fetchone()
        if found:
            raise ValueError('A privately routed source resurfaced in shared content; owner recovery is required before access')


def recover_journal(conn):
    """Replay deny records after content-only restore; no permission is granted."""
    entries = journal_state(conn)
    if not entries:
        return
    known = {row[0] for row in conn.execute("SELECT source_key FROM recall_capture_policy WHERE mode='off'")}
    if entries <= known and conn.execute('SELECT 1 FROM recall_policy_journal').fetchone():
        return
    with journal_lock(conn):
        if not conn.in_transaction:
            conn.execute('BEGIN IMMEDIATE')
        # Re-read under both locks: an acknowledged grant cannot be overwritten
        # by a recovery snapshot taken before the grant.
        entries = journal_state(conn)
        known = {row[0] for row in conn.execute("SELECT source_key FROM recall_capture_policy WHERE mode='off'")}
        for key in entries - known:
            conn.execute("INSERT INTO recall_capture_policy VALUES(?,'off',?) ON CONFLICT(source_key) DO UPDATE SET mode='off'",
                         (key, datetime.now(timezone.utc).isoformat()))
        conn.execute('INSERT OR IGNORE INTO recall_policy_journal VALUES(1,1)')
        conn.commit()


PRIVATE_GUARDS = {}
for _table, _expression in [('memory_sources','NEW.source_key'),('memory_blocks','NEW.source_key'),
    ('memory_rebuild_blocks','NEW.source_key'),('memory_revisions','NEW.source_key'),
    ('sessions',"'claude:' || NEW.session_id"),('exchanges',"'claude:' || NEW.session_id"),
    ('tags',"'claude:' || NEW.session_id"),('highlights',"'claude:' || NEW.session_id")]:
    for _operation in ('INSERT','UPDATE'):
        _name='recall_private_'+_table+'_'+_operation.lower()
        PRIVATE_GUARDS[_name]=f"""CREATE TRIGGER {_name} BEFORE {_operation} ON {_table}
            WHEN {_expression} <> (SELECT owner FROM recall_private_owner)
            BEGIN SELECT RAISE(ABORT,'Foreign source refused by private store'); END"""


def check_private_owner(conn, owner=None):
    """Internal trusted-connection binding. Never accept owner from tool arguments.

    Plain CLI/reader access to a tagged private store is refused. Host filesystem
    protection is separate: this does not authenticate arbitrary same-user Python.
    """
    present = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='recall_private_owner'").fetchone()
    if not present:
        if owner is not None or conn.execute('PRAGMA application_id').fetchone()[0] == PRIVATE_APPLICATION_ID:
            raise ValueError('Private owner binding is missing; access refused')
        return
    rows = conn.execute('SELECT owner FROM recall_private_owner').fetchall()
    if owner is None or len(rows) != 1 or rows[0][0] != owner:
        raise ValueError('This store requires its bound private reader; access refused')
    guards = dict(conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'").fetchall())
    normalize = lambda sql: ' '.join(sql.split()).casefold()
    if any(normalize(guards.get(name) or '') != normalize(sql) for name,sql in PRIVATE_GUARDS.items()):
        raise ValueError('Private store guards are incomplete; access refused')
    if conn.execute('SELECT 1 FROM memory_sources WHERE source_key<>? LIMIT 1',(owner,)).fetchone() or conn.execute("SELECT 1 FROM sessions WHERE 'claude:' || session_id<>? LIMIT 1",(owner,)).fetchone():
        raise ValueError('Private store contains a foreign source; access refused')


def native_identity(path, agent):
    """Positive identity within the adapter's bounded metadata window; no filename guess."""
    with open(path,'rb') as stream:
        for _ in range(30):
            line=stream.readline(256 * 1024 + 1)
            if len(line) > 256 * 1024:
                return None  # Unknown identity: restrictive callers fail closed; ordinary shared capture keeps progressing.
            if not line:break
            try:item=json.loads(line)
            except (ValueError,UnicodeDecodeError):continue
            if not isinstance(item,dict):continue
            if agent=='codex' and item.get('type')=='session_meta':
                value=item.get('payload')
                value=(value.get('id') or value.get('session_id')) if isinstance(value,dict) else None
            elif agent=='claude':value=item.get('sessionId')
            else:value=None
            if isinstance(value,str) and value:return value
    return None


def capture_identities(conn,path,agent,declared):
    actual=native_identity(path,agent)
    if actual is None and (any(key.startswith(agent+':') for key in journal_state(conn)) or conn.execute("SELECT 1 FROM recall_capture_policy WHERE mode='off' AND source_key LIKE ? LIMIT 1",(agent+':%',)).fetchone()):
        raise ValueError('Cannot establish transcript identity under restrictive capture policy; no filename or --session override can grant capture')
    return {agent+':'+declared} | ({agent+':'+actual} if actual else set())
