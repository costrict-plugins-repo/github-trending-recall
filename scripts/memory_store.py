"""Durable redacted blocks, searchable passages, and resumable trace adapters.

No network or model calls. Display budgets never alter the retained source text.
The legacy exchange store remains available while old transcripts are backfilled.
"""
import hashlib
import json
import sqlite3
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from utils import redact_secrets

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS memory_sources (
        source_key TEXT PRIMARY KEY, session_id TEXT NOT NULL, agent TEXT NOT NULL,
        path TEXT NOT NULL, project_path TEXT NOT NULL, repo_id TEXT NOT NULL,
        byte_offset INTEGER NOT NULL DEFAULT 0, source_size INTEGER NOT NULL DEFAULT 0,
        last_indexed_at TEXT, state TEXT NOT NULL DEFAULT 'pending', error TEXT,
        omitted INTEGER NOT NULL DEFAULT 0, malformed INTEGER NOT NULL DEFAULT 0,
        tail_hash TEXT, tail_size INTEGER NOT NULL DEFAULT 0,
        excluded INTEGER NOT NULL DEFAULT 0, metadata_records INTEGER NOT NULL DEFAULT 0,
        unsupported_types TEXT NOT NULL DEFAULT '[]',
        generation INTEGER NOT NULL DEFAULT 0, head_hash TEXT)""",
    """CREATE TABLE IF NOT EXISTS memory_blocks (
        id TEXT PRIMARY KEY, source_key TEXT NOT NULL REFERENCES memory_sources(source_key) ON DELETE CASCADE,
        message_key TEXT NOT NULL, seq INTEGER NOT NULL, role TEXT NOT NULL,
        kind TEXT NOT NULL, timestamp TEXT NOT NULL, text TEXT NOT NULL,
        start_byte INTEGER NOT NULL, end_byte INTEGER NOT NULL, content_hash TEXT NOT NULL,
        ordinal INTEGER NOT NULL DEFAULT 0, generation INTEGER NOT NULL DEFAULT 0)""",
    "CREATE INDEX IF NOT EXISTS memory_blocks_source ON memory_blocks(source_key, seq, ordinal)",
    # Per-message lookup during capture; without it every new message scanned all
    # blocks of the session (328 MB transcript: 2 s per hook pass, 7 s worst).
    "CREATE INDEX IF NOT EXISTS memory_blocks_message ON memory_blocks(source_key, message_key)",
    """CREATE TABLE IF NOT EXISTS memory_chunks (
        id INTEGER PRIMARY KEY, block_id TEXT NOT NULL REFERENCES memory_blocks(id) ON DELETE CASCADE,
        ordinal INTEGER NOT NULL, start_char INTEGER NOT NULL, end_char INTEGER NOT NULL,
        text TEXT NOT NULL, UNIQUE(block_id, ordinal))""",
    """CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
        text, content=memory_chunks, content_rowid=id, tokenize='porter unicode61')""",
    """CREATE TRIGGER IF NOT EXISTS memory_chunks_insert AFTER INSERT ON memory_chunks BEGIN
        INSERT INTO memory_fts(rowid,text) VALUES(new.id,new.text); END""",
    """CREATE TRIGGER IF NOT EXISTS memory_chunks_delete AFTER DELETE ON memory_chunks BEGIN
        INSERT INTO memory_fts(memory_fts,rowid,text) VALUES('delete',old.id,old.text); END""",
    """CREATE TABLE IF NOT EXISTS memory_vectors (
        chunk_id INTEGER PRIMARY KEY REFERENCES memory_chunks(id) ON DELETE CASCADE,
        model TEXT NOT NULL, vector TEXT NOT NULL)""",
]


# Record types that carry no conversation content at all (expected, harmless).
_METADATA_TYPES = {
    'claude': {'attachment', 'last-prompt', 'mode', 'permission-mode', 'ai-title', 'system',
               'summary', 'file-history-snapshot', 'queue-operation', 'progress', 'custom-title'},
    'codex': {'session_meta', 'turn_context', 'token_usage_record', 'world_state', 'event_msg'},
}
# Record/payload types whose content exists but is deliberately NOT retained.
_EXCLUDED_PAYLOADS = {'function_call_output', 'custom_tool_call_output', 'reasoning', 'local_shell_call_output'}


def initialize(conn):
    for statement in SCHEMA:
        conn.execute(statement)
    # v7: classify skipped records (excluded by policy / metadata / unsupported)
    columns = {row[1] for row in conn.execute('PRAGMA table_info(memory_sources)')}
    for name, ddl in (('excluded', 'INTEGER NOT NULL DEFAULT 0'),
                      ('metadata_records', 'INTEGER NOT NULL DEFAULT 0'),
                      ('unsupported_types', "TEXT NOT NULL DEFAULT '[]'"),
                      ('generation', 'INTEGER NOT NULL DEFAULT 0'),
                      ('head_hash', 'TEXT'),
                      # v9 (R07): an explicit `rescope` pins repo_id/project_path against
                      # the cwd observed in the transcript on later index passes.
                      ('scope_pinned', 'INTEGER NOT NULL DEFAULT 0')):
        if name not in columns:
            conn.execute(f'ALTER TABLE memory_sources ADD COLUMN {name} {ddl}')
    # v7: in-message block order (text -> tool call -> text) was only implicit
    # in the id hash, so neighbours and exports listed a turn's blocks in hash order.
    block_columns = {row[1] for row in conn.execute('PRAGMA table_info(memory_blocks)')}
    if 'ordinal' not in block_columns:
        conn.execute('ALTER TABLE memory_blocks ADD COLUMN ordinal INTEGER NOT NULL DEFAULT 0')
    # v7: rebuild generation, so an interrupted rebuild keeps not-yet-rescanned messages
    if 'generation' not in block_columns:
        conn.execute('ALTER TABLE memory_blocks ADD COLUMN generation INTEGER NOT NULL DEFAULT 0')
    # v9, after the column exists on v6 stores. Rebuild bookkeeping (R06/R08): "any block
    # older than the source generation?" and "highest seq in the current generation" are
    # both index seeks with this.
    conn.execute('CREATE INDEX IF NOT EXISTS memory_blocks_generation ON memory_blocks(source_key, generation, seq)')
    migrate_vectors_to_blob(conn)
    import memory_revisions
    memory_revisions.initialize(conn)


def migrate_vectors_to_blob(conn):
    """v9 (P14): vectors are packed little-endian float32 blobs. Pre-v9 rows are
    JSON text; convert them in place. Vectors are derived data, so any row that
    is not a non-empty array of finite float32 numbers is dropped, never
    reinterpreted, and never aborts the migration (R3-02). Dimension policy: the
    first valid row of a model fixes that model's dimension; later rows that
    disagree are dropped. The semantic configuration (if the store has one) is
    migrated too: format `f32le-v1` and the configured model's dimension, NULL
    when its index is empty (R3-03). Returns (converted, dropped)."""
    from semantic_memory import pack_vector, VECTOR_FORMAT
    dims, converted, dropped = {}, 0, 0
    for r in conn.execute("SELECT model, length(vector)/4 FROM memory_vectors WHERE typeof(vector)='blob'"):
        dims.setdefault(r[0], r[1])
    rows = conn.execute("SELECT chunk_id, model, vector FROM memory_vectors WHERE typeof(vector)='text'").fetchall()
    for chunk_id, model, text in rows:
        try:
            values = json.loads(text)
            blob = pack_vector(values)
            if dims.setdefault(model, len(values)) != len(values):
                raise ValueError('dimension mismatch')
        except (ValueError, TypeError, OverflowError):
            conn.execute('DELETE FROM memory_vectors WHERE chunk_id=?', (chunk_id,))
            dropped += 1
            continue
        conn.execute('UPDATE memory_vectors SET vector=? WHERE chunk_id=?', (sqlite3.Binary(blob), chunk_id))
        converted += 1
    for model, dim in dims.items():   # blob rows that disagree with the model's dimension
        dropped += conn.execute('DELETE FROM memory_vectors WHERE model=? AND length(vector)!=?', (model, dim * 4)).rowcount
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_semantic_config'").fetchone():
        columns = {r[1] for r in conn.execute('PRAGMA table_info(memory_semantic_config)')}
        if 'format' not in columns:
            conn.execute('ALTER TABLE memory_semantic_config ADD COLUMN format TEXT')
        if 'dimension' not in columns:
            conn.execute('ALTER TABLE memory_semantic_config ADD COLUMN dimension INTEGER')
        row = conn.execute('SELECT model FROM memory_semantic_config WHERE id=1').fetchone()
        if row:
            conn.execute('UPDATE memory_semantic_config SET format=?, dimension=? WHERE id=1', (VECTOR_FORMAT, dims.get(row[0])))
    return converted, dropped


FTS_TABLES = ('memory_fts', 'exchanges_fts')
_EXPECTED_SCHEMA = None


def expected_schema():
    """(tables -> column names, index names, trigger names) of a store created by
    this code, read from a throwaway pristine store so the check covers every
    runtime object, not a hand-kept list (Astra, integration verification)."""
    global _EXPECTED_SCHEMA
    if _EXPECTED_SCHEMA is None:
        import tempfile
        import db as _db   # lazy: db imports this module during migrations
        with tempfile.TemporaryDirectory(prefix='recall-schema-') as d:
            c = _db.get_connection(Path(d) / 'pristine.db')
            tables, indexes, triggers = {}, set(), set()
            for typ, name in c.execute("SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall():
                if typ == 'table':
                    tables[name] = {r[1] for r in c.execute(f'PRAGMA table_info("{name}")')}
                elif typ == 'index':
                    indexes.add(name)
                elif typ == 'trigger':
                    triggers.add(name)
            c.close()
        _EXPECTED_SCHEMA = (tables, frozenset(indexes), frozenset(triggers))
    return _EXPECTED_SCHEMA


def verify_schema(conn):
    """List what is missing or unusable in a store that claims the current schema; [] when healthy.
    Every table, column, index and trigger of a pristine store must be present; both FTS
    indexes must answer a query and pass the external-content integrity check (so an
    index that no longer matches its content table is reported); foreign keys consistent."""
    tables, indexes, triggers = expected_schema()
    problems = []
    have = {(r[0], r[1]) for r in conn.execute("SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}
    for table in sorted(tables):
        if ('table', table) not in have:
            problems.append('missing table ' + table)
            continue
        actual = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
        problems += [f'missing column {table}.{c}' for c in sorted(tables[table] - actual)]
    problems += ['missing index ' + i for i in sorted(indexes) if ('index', i) not in have]
    problems += ['missing trigger ' + t for t in sorted(triggers) if ('trigger', t) not in have]
    for fts in FTS_TABLES:
        if ('table', fts) in have:
            try:
                conn.execute(f"SELECT count(*) FROM {fts} WHERE {fts} MATCH 'a'").fetchone()
                conn.execute(f"INSERT INTO {fts}({fts}, rank) VALUES('integrity-check', 1)")
            except sqlite3.Error as exc:
                problems.append(f'{fts} unusable or out of sync with its content: {exc}')
    try:
        if conn.execute('PRAGMA foreign_key_check').fetchall():
            problems.append('foreign_key_check reports orphan rows')
    except sqlite3.Error as exc:
        problems.append('foreign_key_check failed: ' + str(exc))
    if not problems:
        import memory_revisions
        problems.extend(memory_revisions.verify_content(conn))
    return problems


def repair_schema(conn):
    """Recreate missing derived objects: legacy and durable tables/indexes/triggers
    via their idempotent DDL, and FTS indexes rebuilt from their content tables when
    missing or out of sync. Columns are NOT invented (a store missing one is still
    rejected by verify_schema). Returns the problems that went away."""
    import db as _db
    before = set(verify_schema(conn))
    conn.executescript(_db._SCHEMA_SQL)
    initialize(conn)
    from recall_privacy import initialize as initialize_privacy
    initialize_privacy(conn)
    for fts in FTS_TABLES:
        if any(p.startswith(fts) or p == 'missing table ' + fts or p.startswith('missing table ' + fts + '_') for p in before):
            try:
                conn.execute(f"INSERT INTO {fts}({fts}) VALUES('rebuild')")
            except sqlite3.Error:
                pass
    return sorted(before - set(verify_schema(conn)))


def classify_skipped(entry, agent):
    """Why a record produced no block: 'metadata' (no content by nature),
    'excluded' (content deliberately not retained: tool results, reasoning,
    mirrored events, Codex compaction summaries, Claude isMeta bodies, developer/system messages, thinking/image-only turns)
    or 'unsupported' (a shape the adapter does not recognise)."""
    typ = entry.get('type')
    # Observed Claude host bookkeeping in real Code rollouts. These carry no
    # conversation text; unknown future kinds remain unsupported below.
    if agent == 'claude' and typ in {'worktree-state', 'relocated', 'atis-latch',
                                    'bridge-session', 'file-history-delta', 'frame-link', 'cost-state', 'pr-link'}:
        return 'metadata'
    if typ in _METADATA_TYPES.get(agent, set()):
        return 'metadata'
    if agent == 'codex':
        # Host-generated summaries/replacement histories duplicate prior context;
        # they are not original conversation evidence. Observed on a live desktop rollout.
        if typ == 'compacted':
            return 'excluded'
        payload = entry.get('payload') if isinstance(entry.get('payload'), dict) else {}
        ptype = payload.get('type')
        if typ == 'response_item' and (ptype in _EXCLUDED_PAYLOADS or payload.get('channel') == 'analysis'
                                       or payload.get('role') in ('developer', 'system')):
            return 'excluded'
        if typ == 'response_item' and ptype == 'message':
            return 'excluded'          # a message whose blocks were all non-text (images etc.)
        return 'unsupported'
    if agent != 'codex' and entry.get('isMeta'):
        return 'excluded'          # rendered skill/command body (P65)
    message = entry.get('message') if isinstance(entry.get('message'), dict) else None
    if typ in ('user', 'assistant') and message is not None:
        content = message.get('content')
        if isinstance(content, list):
            kinds = {b.get('type') for b in content if isinstance(b, dict)}
            if kinds and kinds <= {'fallback'}:
                return 'metadata'  # host model-routing notice, no user/assistant prose
            if kinds and kinds <= {'tool_result', 'thinking', 'redacted_thinking', 'image', 'document'}:
                return 'excluded'
        return 'excluded' if content in (None, '', []) else 'unsupported'
    return 'unsupported'


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def repository_identity(cwd):
    """Unify worktrees and equivalent SSH/HTTPS remotes without storing credentials."""
    path = str(Path(cwd or '.').expanduser().resolve())
    def git(*args):
        return subprocess.check_output(['git', '-C', path, *args], stderr=subprocess.DEVNULL,
                                       timeout=2, text=True).strip()
    try:
        remote = git('config', '--get', 'remote.origin.url')
        if '://' in remote:
            url = urlsplit(remote)
            host, repo = (url.hostname or '').lower(), url.path.strip('/')
        elif re.match(r'[^/]+@[^:]+:', remote):
            host, repo = remote.split('@', 1)[1].split(':', 1)
        else:
            host, repo = 'local', str(Path(path, remote).resolve())
        return (host + '/' + repo.removesuffix('.git')).rstrip('/')
    except (subprocess.SubprocessError, OSError, ValueError):
        try:
            common = git('rev-parse', '--git-common-dir')
            return 'local-git:' + digest(str(Path(path, common).resolve()))[:24]
        except (subprocess.SubprocessError, OSError):
            return 'directory:' + digest(path)[:24]


def split_passages(text, size=1600, overlap=160):
    """Return exact source slices; offsets make reassembly unambiguous."""
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            boundary = max(text.rfind('\n', start + size // 2, end),
                           text.rfind(' ', start + size // 2, end))
            if boundary > start:
                end = boundary + 1
        yield start, end, text[start:end]
        if end == len(text):
            break
        start = max(start + 1, end - overlap)


def normalize_record(entry, agent):
    """Yield (role, kind, text, ordinal); never capture instructions or reasoning.

    Codex event_msg records mirror response_item records, so only response_item
    (or the older top-level message layout) is indexed. Tool results stay excluded.
    """
    if agent == 'codex':
        if entry.get('type') == 'response_item':
            message = entry.get('payload') or {}
        elif entry.get('type') == 'message':
            message = entry
        else:
            return
        if not isinstance(message, dict):
            return
        typ = message.get('type')
        if typ in ('function_call', 'custom_tool_call'):
            value = message.get('arguments', message.get('input', ''))
            yield 'assistant', 'tool_use', str(message.get('name') or 'tool') + ' ' + (
                value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)), 0
            return
    else:
        if entry.get('isMeta'):
            # Claude Code writes the rendered body of a skill or slash command as a
            # user record flagged isMeta: host instructions, not the user's words.
            # Excluded by policy like system/developer messages (P65).
            return
        message = entry.get('message') or {}
    if not isinstance(message, dict):
        return
    role = message.get('role') or entry.get('type')
    if role not in ('user', 'assistant') or message.get('channel') == 'analysis':
        return
    if agent != 'codex' and entry.get('isCompactSummary'):
        # The host's compaction summary: retained and searchable, but presented under
        # role 'host' so brief/compaction never quote it as the user's own words.
        role = 'host'
    content = message.get('content') or []
    if isinstance(content, str):
        content = [{'type': 'text', 'text': content}]
    if not isinstance(content, list):
        return   # R09: an integer/object content field is an unsupported shape, not a crash
    for ordinal, block in enumerate(content):
        if not isinstance(block, dict):
            continue
        kind = block.get('type')
        if kind in ('text', 'input_text', 'output_text'):
            text = block.get('text', '')
            if isinstance(text, str) and text:
                yield role, 'text', text, ordinal
        elif kind == 'tool_use' and role == 'assistant':
            yield role, 'tool_use', str(block.get('name') or 'tool') + ' ' + json.dumps(
                block.get('input', {}), ensure_ascii=False, sort_keys=True, default=str), ordinal


def _store_block(conn, source_key, key, seq, role, kind, timestamp, text, ordinal, start, end, generation=0, staging=False):
    text = re.sub(r'data:[^\s,]*;base64,[A-Za-z0-9+/=]+', '[ELIDED:base64]', text)
    text = redact_secrets(text)
    block_id = digest(f'{source_key}:{key}:{ordinal}:{kind}')[:32]
    row = dict(id=block_id,source_key=source_key,message_key=key,seq=seq,role=role,kind=kind,
               timestamp=timestamp,text=text,start_byte=start,end_byte=end,content_hash=digest(text),
               ordinal=ordinal,generation=generation)
    return _put_block(conn,row,staging)


def _put_block(conn, row, staging=False):
    from memory_revisions import FIELDS, COLUMNS, remember
    table = 'memory_rebuild_blocks' if staging else 'memory_blocks'
    existing=conn.execute('SELECT * FROM '+table+' WHERE id=?',(row['id'],)).fetchone()
    unchanged=existing is not None and existing['content_hash']==row['content_hash']
    if not staging and existing and not unchanged:
        remember(conn,existing)
        conn.execute('DELETE FROM memory_chunks WHERE block_id=?',(row['id'],))
    if unchanged:
        conn.execute('UPDATE '+table+' SET generation=?,start_byte=?,end_byte=?,seq=?,role=?,timestamp=? WHERE id=?',
                     (row['generation'],row['start_byte'],row['end_byte'],row['seq'],row['role'],row['timestamp'],row['id']))
    else:
        conn.execute('INSERT INTO '+table+' ('+COLUMNS+') VALUES ('+','.join('?' for _ in FIELDS)+') '
                     'ON CONFLICT(id) DO UPDATE SET '+','.join(k+'=excluded.'+k for k in FIELDS if k not in ('id','source_key','message_key')),
                     tuple(row[k] for k in FIELDS))
    if not staging and not unchanged:
        for i,(a,b,passage) in enumerate(split_passages(row['text'])):
            conn.execute('INSERT INTO memory_chunks(block_id,ordinal,start_char,end_char,text) VALUES(?,?,?,?,?)',
                         (row['id'],i,a,b,passage))
    return int(not unchanged)


def transcript_paths(root, agent='claude', recursive=False):
    """Candidates within an explicit directory, respecting each agent's layout.

    Claude project folders have main transcripts at their top level. Codex's
    normal sessions root uses year/month/day directories, which are not subagents.
    """
    root = Path(root)
    paths = root.rglob('*.jsonl') if recursive or agent == 'codex' else root.glob('*.jsonl')
    return (path for path in paths if path.is_file() and not path.is_symlink())


def looks_like_transcript(path, agent='claude', probe=30):
    """True when one of the first records is a conversation message. Workflow
    journals and other JSONL sidecars under a project directory are not sources."""
    try:
        with open(path, 'rb') as file:
            for _ in range(probe):
                raw = file.readline()
                if not raw:
                    break
                try:
                    item = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(item, dict):
                    continue
                if agent == 'codex':
                    if item.get('type') in ('session_meta', 'response_item', 'message'):
                        return True
                elif isinstance(item.get('message'), dict) or item.get('type') in ('user', 'assistant'):
                    return True
    except OSError:
        return False
    return False


def trace_metadata(path, agent, session_id='', cwd=''):
    """Read metadata from the file, without collecting its message content."""
    if session_id and cwd:
        return session_id, cwd
    with open(path, 'rb') as file:
        for _ in range(30):
            raw = file.readline()
            if not raw:
                break
            try:
                item = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(item, dict):
                continue
            if agent == 'codex' and item.get('type') == 'session_meta':
                meta = item.get('payload') or {}
                session_id = session_id or meta.get('id') or meta.get('session_id', '')
                cwd = cwd or meta.get('cwd', '')
                break
            if agent == 'claude':
                session_id = session_id or item.get('sessionId', '')
                cwd = cwd or item.get('cwd', '')
            if session_id and cwd:
                break
    return session_id or Path(path).stem, cwd or str(Path(path).parent)


def index_file(conn, path, agent='claude', session_id='', cwd='', max_bytes=2*1024*1024,
               max_records=1000, rebuild=False, publish_rebuild=True):
    """One committed-by-caller scan; reruns upsert stable identities, never duplicate.

    Each source has its own cursor. Missing/changed sources preserve stored blocks.

    Rebuilds stage a complete replacement outside the published block/FTS tables.
    Only a verified EOF prefix is published, atomically, by an explicit index pass.
    Hooks pass publish_rebuild=False and expose rebuild_ready for maintenance.
    Superseded text is retained by the revision policy; normal append capture keeps
    publishing completed passes. Each read connection pins a SQLite snapshot.

    Edit detection: the 256 bytes before the saved cursor (append continuity)
    and the first 256 bytes of the file (header) are hashed; a mismatch or a
    shrink stops incremental capture with ``source_changed`` and names which
    window changed. Edits between those windows are not detected.

    A partial final JSONL record is held regardless of whether its prefix
    happens to be valid JSON.
    """
    path = str(Path(path).expanduser().resolve())
    from recall_privacy import mode, capture_identities
    session_id, cwd = trace_metadata(path, agent, session_id, cwd)
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    identities = capture_identities(conn,path,agent,session_id)
    if agent == 'claude' and Path(path).parent.name == 'subagents':
        identities.add(agent + ':' + session_id + '/' + Path(path).stem)
    if any(mode(conn, key) == 'off' for key in identities):
        return {'source': agent + ':' + session_id, 'blocks': 0, 'offset': 0,
                'state': 'capture_disabled', 'next_action': 'Owner disabled new capture; existing retained history is unchanged.'}

    source_key = agent + ':' + session_id
    if agent == 'claude' and Path(path).parent.name == 'subagents':
        # A subagent transcript carries its PARENT's sessionId. Key it as its own
        # source so it never masquerades as the parent (P72).
        source_key += '/' + Path(path).stem
    previous = conn.execute('SELECT * FROM memory_sources WHERE source_key=?', (source_key,)).fetchone()
    if previous and previous['path'] != path and os.path.exists(previous['path']):
        # Same identity, different file, and the registered file still exists: this
        # is another file claiming the source (a sidecar, a copy, a journal). Refuse
        # rather than reinterpret the registered source's cursor against a different
        # file, which marked live sources source_changed during a directory import (P72).
        return {'source': source_key, 'blocks': 0, 'offset': previous['byte_offset'], 'state': 'path_conflict',
                'registered_path': previous['path'], 'offered_path': path}
    if previous and previous['scope_pinned']:
        # R07: an explicit `rescope` wins over the cwd recorded in the transcript.
        cwd, repo = previous['project_path'], previous['repo_id']
    else:
        repo = previous['repo_id'] if previous and previous['project_path'] == cwd else repository_identity(cwd)
    conn.execute('''INSERT INTO memory_sources(source_key,session_id,agent,path,project_path,repo_id)
                    VALUES(?,?,?,?,?,?) ON CONFLICT(source_key) DO NOTHING''',
                 (source_key, session_id, agent, path, cwd, repo))
    previous = conn.execute('SELECT * FROM memory_sources WHERE source_key=?', (source_key,)).fetchone()
    offset = previous['byte_offset']
    size = os.path.getsize(path)
    generation = previous['generation'] or 0
    if rebuild:
        generation += 1
        from memory_revisions import published_source
        base = published_source(previous)
        target = dict(path=path,project_path=cwd,repo_id=repo)
        conn.execute('DELETE FROM memory_rebuild_blocks WHERE source_key=?',(source_key,))
        conn.execute('DELETE FROM memory_rebuild_segments WHERE source_key=?',(source_key,))
        conn.execute("UPDATE memory_sources SET byte_offset=0,omitted=0,malformed=0,tail_size=0,excluded=0,"
                     "metadata_records=0,unsupported_types='[]',generation=?,state='rebuilding',head_hash=NULL,"
                     "rebuild_in_progress=1,rebuild_target=?,rebuild_base=? WHERE source_key=?",
                     (generation,json.dumps(target),json.dumps(base),source_key))
        offset = 0
    previous = conn.execute('SELECT * FROM memory_sources WHERE source_key=?',(source_key,)).fetchone()
    rebuilding = bool(previous['rebuild_in_progress'])
    if rebuilding:
        target=json.loads(previous['rebuild_target'])
        cwd,repo=target['project_path'],target['repo_id']
    if rebuilding and not rebuild and previous['state']=='source_changed' and (previous['error'] or '').startswith('Rebuild '):
        return {'source':source_key,'blocks':0,'offset':offset,'state':'source_changed','changed':previous['error']}
    block_table='memory_rebuild_blocks' if rebuilding else 'memory_blocks'
    message_index='memory_rebuild_message' if rebuilding else 'memory_blocks_message'
    changed = None
    if size < offset:
        changed = 'file shrank below the saved cursor'
    if offset and not changed and previous['tail_size']:
        with open(path, 'rb') as file:
            file.seek(offset - previous['tail_size'])
            if hashlib.sha256(file.read(previous['tail_size'])).hexdigest() != previous['tail_hash']:
                changed = 'bytes just before the saved cursor changed'
    if offset and not changed and previous['head_hash'] and size >= 256:
        with open(path, 'rb') as file:
            if hashlib.sha256(file.read(256)).hexdigest() != previous['head_hash']:
                changed = 'file header (first 256 bytes) changed'
    if changed and not rebuild:
        conn.execute("UPDATE memory_sources SET state='source_changed',error=?,source_size=? WHERE source_key=?",
                     ('Source changed before the saved cursor (%s); use explicit rebuild.' % changed, size, source_key))
        return {'source': source_key, 'blocks': 0, 'offset': offset, 'state': 'source_changed', 'changed': changed}
    original = offset
    count = omitted = malformed = excluded = metadata = 0
    unsupported_types = set() if rebuild else set(json.loads(previous['unsupported_types'] or '[]'))
    state = 'complete'
    seq = conn.execute('SELECT COALESCE(MAX(seq),0) FROM '+block_table+' WHERE source_key=? AND generation=?',
                       (source_key, generation)).fetchone()[0]
    segment_hash=hashlib.sha256()
    with open(path, 'rb') as file:
        file.seek(offset)
        for record_number in range(max_records):
            start = file.tell()
            raw = file.readline()
            if not raw:
                break
            if not raw.endswith(b'\n'):
                state = 'partial_record'
                break
            if start > original and file.tell() - original > max_bytes:
                state = 'backlog'
                break
            offset = file.tell()
            segment_hash.update(raw)
            try:
                entry = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                malformed += 1
                continue
            if not isinstance(entry, dict):
                malformed += 1
                continue
            payload = entry.get('payload') or {}
            if not isinstance(payload, dict):
                payload = {}
            key = str(entry.get('uuid') or payload.get('id') or payload.get('call_id') or start)
            # INDEXED BY: with a generation predicate the planner preferred the
            # (source_key, generation, seq) index and scanned every block of the
            # generation per message (the P33 regression, re-introduced by R08's
            # generation check). The message index is always the right one here.
            record_seq = None
            for row in conn.execute('SELECT seq, generation FROM '+block_table+' INDEXED BY '+message_index+' '
                                    'WHERE source_key=? AND message_key=?', (source_key, key)):
                if row[1] == generation:
                    record_seq = row[0]
                    break
            if record_seq is None:
                record_seq = seq + 1
            try:
                blocks = list(normalize_record(entry, agent))
            except (TypeError, AttributeError, ValueError, KeyError):
                malformed += 1   # R09: valid JSON, unsupported field types; counted, skipped, cursor advances
                continue
            if not blocks:
                why = classify_skipped(entry, agent)
                if why == 'metadata':
                    metadata += 1
                elif why == 'excluded':
                    excluded += 1
                else:
                    omitted += 1
                    if len(unsupported_types) < 8:
                        unsupported_types.add(str(entry.get('type')))
            for role, kind, text, ordinal in blocks:
                count += _store_block(conn, source_key, key, record_seq, role, kind,
                                      str(entry.get('timestamp') or ''), text, ordinal, start, offset,
                                      generation=generation, staging=rebuilding)
            if blocks:
                seq = max(seq, record_seq)
        else:
            state = 'backlog'
        width = min(offset, 256)
        file.seek(offset-width)
        tail_hash = hashlib.sha256(file.read(width)).hexdigest()
        file.seek(0)
        head_hash = hashlib.sha256(file.read(256)).hexdigest() if size >= 256 else None
    if state == 'complete' and offset < size:
        state = 'backlog'
    if offset >= size:
        state = 'complete'
    stale = 0
    if rebuilding:
        if offset>original:
            conn.execute('INSERT INTO memory_rebuild_segments VALUES(?,?,?,?)',
                         (source_key,original,offset,segment_hash.hexdigest()))
        if state == 'complete' and publish_rebuild:
            import memory_revisions
            try:
                stale=memory_revisions.publish(conn,source_key,path,offset,_put_block)
            except ValueError as exc:
                conn.execute("UPDATE memory_sources SET state='source_changed',error=? WHERE source_key=?",(str(exc),source_key))
                return {'source':source_key,'blocks':count,'offset':offset,'state':'source_changed','changed':str(exc)}
        else:
            state='rebuild_ready' if state=='complete' else 'rebuilding'
            # Published source identity changes only with the replacement.
            cwd,repo=previous['project_path'],previous['repo_id']
    now = datetime.now(timezone.utc).isoformat()
    conn.execute('''UPDATE memory_sources SET path=?,project_path=?,repo_id=?,byte_offset=?,source_size=?,
        last_indexed_at=?,state=?,error=NULL,omitted=omitted+?,malformed=malformed+?,tail_hash=?,tail_size=?,
        excluded=excluded+?,metadata_records=metadata_records+?,unsupported_types=?,generation=?,head_hash=?
        WHERE source_key=?''', (path,cwd,repo,offset,size,now,state,omitted,malformed,tail_hash,width,
                              excluded,metadata,json.dumps(sorted(unsupported_types)),generation,head_hash,source_key))
    return {'source': source_key, 'blocks': count, 'offset': offset, 'state': state, 'stale_removed': stale}


DEFAULT_HALF_LIFE_DAYS = 30   # P12: frozen after the development-split experiment; 0 disables


def search(conn, query, limit=5, repo_id=None, source_key=None, since=None, until=None,
           half_life=DEFAULT_HALF_LIFE_DAYS, require_all=False, kind='text'):
    """BM25 candidate retrieval, optional recency, diverse blocks with exact get refs."""
    stopwords=set('a an the and or of to in on for with by is are was were be been do does did how why what which when where who we i you it this that our your from as at have has had can could would should earlier previous'.split())
    terms = [term for term in re.findall(r'[\w./:-]+', query, flags=re.UNICODE)
             if term.lower() not in stopwords][:64]
    if not terms or limit < 1:
        return []
    # Queries from users are literals, never interpreted as FTS control syntax.
    quoted = ['"'+term.replace('"','""')+'"' for term in terms]
    match = (' AND ' if require_all else ' OR ').join(quoted)
    clauses, args = ['memory_fts MATCH ?'], [match]
    if kind:
        clauses.append('b.kind=?'); args.append(kind)
    for field, value in [('s.repo_id',repo_id),('s.source_key',source_key)]:
        if value:
            clauses.append(field+'=?'); args.append(value)
    if since:
        clauses.append('b.timestamp>=?'); args.append(since)
    if until:
        clauses.append('b.timestamp<?'); args.append(until)
    sql = '''SELECT c.id AS chunk_id,c.block_id,c.start_char,c.end_char,c.text,
        b.seq,b.role,b.kind,b.timestamp,b.content_hash,s.agent,s.session_id,s.source_key,s.repo_id,s.project_path,
        bm25(memory_fts) AS rank,snippet(memory_fts,0,'«','»','…',40) AS snippet
        FROM memory_fts JOIN memory_chunks c ON c.id=memory_fts.rowid
        JOIN memory_blocks b ON b.id=c.block_id JOIN memory_sources s ON s.source_key=b.source_key
        WHERE ''' + ' AND '.join(clauses) + ' ORDER BY rank,b.timestamp DESC LIMIT ?'
    args.append(max(100, limit*20))
    rows = [dict(row) for row in conn.execute(sql, args)]
    if half_life > 0:
        now = datetime.now(timezone.utc)
        for row in rows:
            try:
                stamp = datetime.fromisoformat(row['timestamp'].replace('Z','+00:00'))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                age = max(0, (now-stamp).total_seconds()/86400)
            except ValueError:
                age = 0
            row['rank'] *= 0.5 + 0.5 * 2**(-age/half_life)
        rows.sort(key=lambda row: row['rank'])
    result, seen = [], set()
    for row in rows:
        if row['block_id'] in seen:
            continue
        seen.add(row['block_id'])
        row['get'] = f"get {row['block_id']} --start {row['start_char']}"
        row['provenance'] = evidence_provenance(row['role'], row['kind'])
        result.append(row)
        if len(result) == limit:
            break
    return result


def evidence_provenance(role, kind):
    if kind == 'tool_use':
        return 'tool_request: records arguments/intention, not successful execution or resulting file state'
    if role == 'host':
        return 'host_summary: generated context, not original participant testimony'
    return role + '_record: may contain pasted reports; role alone does not establish authorship or truth'


def get_block(conn, block_id, start=0, max_chars=8000, neighbors=0, quote=None, expected_hash=None, revision=None):
    if start < 0 or max_chars < 1 or neighbors < 0:
        raise ValueError('start/neighbors must be nonnegative and max_chars positive')
    if revision is not None:
        import memory_revisions
        saved=memory_revisions.revision(conn,block_id,revision)
        if saved is None:
            raise ValueError('Revision unavailable: expired, pruned, or never retained')
        source=conn.execute('SELECT agent,session_id,repo_id,project_path FROM memory_sources WHERE source_key=?',(saved['source_key'],)).fetchone()
        row={**dict(saved),**dict(source)}
    else:
        row = conn.execute('SELECT b.*,s.agent,s.session_id,s.repo_id,s.project_path FROM memory_blocks b '
                           'JOIN memory_sources s ON s.source_key=b.source_key WHERE b.id=?',(block_id,)).fetchone()
    if row is None:
        raise ValueError('Unknown block: '+block_id)
    result = dict(row)
    full = result.pop('text')
    if start > len(full):
        raise ValueError('start exceeds block length')
    end = min(len(full), start+max_chars)
    result.update(text=full[start:end],start_char=start,end_char=end,total_chars=len(full),
                  next_start=end if end<len(full) else None)
    result['revision'] = row['content_hash']
    result['revision_requested'] = revision is not None
    result['provenance'] = evidence_provenance(row['role'], row['kind'])
    if quote is not None or expected_hash is not None:
        if quote is not None and (not isinstance(quote, str) or not quote):
            raise ValueError('quote must be a nonempty string')
        version_matches = expected_hash is None or expected_hash == row['content_hash']
        at = full[start:end].find(quote) if quote is not None else -1
        matched = quote is None or at >= 0
        result['citation_check'] = {
            'valid': version_matches and matched, 'version_matches': version_matches,
            'quote_matches': matched if quote is not None else None,
            'quote_start': start+at if quote is not None and at >= 0 else None,
            'quote_end': start+at+len(quote) if quote is not None and at >= 0 else None,
            'scope': 'Exact text in this returned window and optional content hash only; not claim correctness, authorship or execution success. Retention can expire unpinned revisions.'}
    result['neighbors'] = [dict(r) for r in conn.execute('''SELECT id,seq,role,kind,timestamp,substr(text,1,240) AS preview
        FROM memory_blocks WHERE source_key=? AND seq BETWEEN ? AND ? AND id<>? ORDER BY seq,ordinal,id''',
        (row['source_key'],row['seq']-neighbors,row['seq']+neighbors,block_id))] if neighbors and revision is None else []
    return result


# Wrappers hosts inject into user-role records (Codex plugin lists, Claude Code
# system reminders and slash-command echoes). Retained verbatim like everything
# else, but never presented as the user's objective or as decision evidence.
HOST_METADATA_TAGS = ('recommended_plugins', 'system-reminder', 'command-name', 'command-message', 'command-args',
                      'local-command-stdout', 'local-command-caveat', 'ide_selection', 'ide_opened_file',
                      'task-notification', 'user-memory-input', 'available_plugins', 'environment_context')
_HOST_METADATA_RX = re.compile(r'<(%s)\b[^>]*>.*?</\1\s*>' % '|'.join(re.escape(t) for t in HOST_METADATA_TAGS), re.S)


def prose_segments(text):
    """(start, end) character spans of TEXT that lie outside host-injected metadata
    wrappers, trimmed of surrounding whitespace; [] when the block is metadata only."""
    spans, pos = [], 0
    for m in _HOST_METADATA_RX.finditer(text):
        spans.append((pos, m.start()))
        pos = m.end()
    spans.append((pos, len(text)))
    out = []
    for a, b in spans:
        seg = text[a:b]
        lead, trail = len(seg) - len(seg.lstrip()), len(seg) - len(seg.rstrip())
        if b - trail > a + lead:
            out.append((a + lead, b - trail))
    return out


def is_host_metadata(text):
    return not prose_segments(text)


def brief(conn, repo_id=None, source_key=None, limit=8, since=None):
    """Return selected source evidence, never assert an extracted claim is current.
    Host-injected metadata blocks are skipped; a block that mixes metadata with the
    user's words is excerpted from its prose only, with exact offsets (P13)."""
    where, args = [], []
    for field,value in [('s.repo_id',repo_id),('s.source_key',source_key)]:
        if value:
            where.append(field+'=?'); args.append(value)
    if since:
        where.append('b.timestamp>=?'); args.append(since)
    base = '''SELECT b.id,b.seq,b.role,b.kind,b.timestamp,b.content_hash,b.text,s.agent,s.session_id,s.repo_id
              FROM memory_blocks b JOIN memory_sources s ON s.source_key=b.source_key'''
    # Prose only: tool-call inputs are actions, not decisions, and their text
    # (patches, commands) trips the decision-language regex. They are listed
    # separately as recent_actions.
    prose = where + ["b.kind='text'", "b.role IN ('user','assistant')"]   # never the host's own summary
    scope = ' WHERE ' + ' AND '.join(prose)
    # Fetch a bounded recent pool and preserve the first ask separately.
    rows = [dict(r) for r in conn.execute(base+scope+' ORDER BY b.timestamp DESC,b.seq DESC LIMIT 100',args)
            if not is_host_metadata(r['text'])]
    first = next((r for r in conn.execute(base+scope+" AND b.role='user' ORDER BY b.timestamp,b.seq LIMIT 20",args)
                  if not is_host_metadata(r['text'])), None)
    signal = re.compile(r'\b(decid|reject|because|instead|fixed|resolved|pending|next|blocked|todo|remaining|failed)', re.I)
    ranked = sorted(enumerate(rows),key=lambda ir:(bool(signal.search(ir[1]['text'])), -ir[0]),reverse=True)
    selected = ([dict(first)] if first else []) + rows[:2] + [r for _,r in ranked]
    actions = [dict(r) for r in conn.execute(base+' WHERE '+' AND '.join(where+["b.kind='tool_use'"])+
                                             ' ORDER BY b.timestamp DESC,b.seq DESC LIMIT 5',args)]
    output, seen = [], set()
    for r in selected:
        if r['id'] in seen:
            continue
        seen.add(r['id'])
        text = r.pop('text')
        segments = prose_segments(text)
        (head_start, head_seg_end), (tail_seg_start, tail_end) = segments[0], segments[-1]
        # Excerpts carry exact offsets into the retained block and never cross a host
        # wrapper. A prose span of up to 1,000 chars is one excerpt (a 783-char reply
        # was being cut mid-path into a 500-char head and a 283-char tail); longer
        # spans get a 500-char head and a 500-char tail, each inside one prose segment.
        if len(segments) == 1 and tail_end - head_start <= 1000:
            r['excerpts'] = [{'start_char':head_start,'text':text[head_start:tail_end]}]
        else:
            head_end = min(head_start+500, head_seg_end)
            r['excerpts'] = [{'start_char':head_start,'text':text[head_start:head_end]}]
            tail_start = max(tail_seg_start, tail_end-500, head_end)
            if tail_end > tail_start:
                r['excerpts'].append({'start_char':tail_start,'text':text[tail_start:tail_end]})
        r['total_chars'] = len(text)
        r['provenance'] = evidence_provenance(r['role'], r['kind'])
        if _HOST_METADATA_RX.search(text):   # only when a wrapper was actually removed, not for trimmed whitespace
            r['host_metadata_stripped'] = True
        output.append(r)
        if len(output)>=limit:
            break
    return {'notice':'Selected historical evidence, not a complete summary or verified current state.',
            'evidence':output,'sampled':True,
            'recent_actions':[{'id':a['id'],'timestamp':a['timestamp'],'agent':a['agent'],'session_id':a['session_id'],
                               'content_hash':a['content_hash'],'provenance':evidence_provenance(a['role'], a['kind']),
                               'text':' '.join(a['text'].split())[:200]} for a in actions]}


def index_mapped_file(conn, path, agent='claude', cwd='', **kwargs):
    """Explicit host mapping is a durable pin, never an implicit rescope.

    Hold the writer lock across the scope check and capture. The caller commits
    or rolls back, as for index_file. Background indexing preserves this pin.
    """
    if not cwd:
        return index_file(conn, path, agent=agent, **kwargs)
    cwd = str(Path(cwd).expanduser().resolve())
    path = Path(path).expanduser().resolve()
    sid, _ = trace_metadata(path, agent, kwargs.get('session_id', ''))
    key = agent + ':' + sid
    if agent == 'claude' and path.parent.name == 'subagents':
        key += '/' + path.stem
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    old = conn.execute('SELECT repo_id,byte_offset FROM memory_sources WHERE source_key=?', (key,)).fetchone()
    if old and old['repo_id'] != repository_identity(cwd):
        return {'source': key, 'blocks': 0, 'offset': old['byte_offset'], 'state': 'scope_mismatch',
                'next_action': 'Existing source belongs to another repository; inspect it and use explicit rescope first.'}
    result = index_file(conn, path, agent=agent, cwd=cwd, **kwargs)
    if result['state'] not in ('path_conflict', 'capture_disabled'):
        conn.execute('UPDATE memory_sources SET scope_pinned=1 WHERE source_key=?', (key,))
    return result


def status(conn, repo_id=None, limit=20, offset=0, source_key=None):
    """Source listing. `source_key` narrows to one source (R11: search/brief pass
    the same filter they retrieve with, so coverage describes what was searched)."""
    sql='SELECT * FROM memory_sources'
    if source_key:
        scope, args = ' WHERE source_key=?', (source_key,)
    else:
        scope=(' WHERE repo_id=?' if repo_id else '')
        args=(repo_id,) if repo_id else ()
    total=conn.execute('SELECT count(*) FROM memory_sources'+scope,args).fetchone()[0]
    agents=dict(conn.execute('SELECT agent,count(*) FROM memory_sources'+scope+' GROUP BY agent',args).fetchall())
    rows=conn.execute(sql+scope+' ORDER BY last_indexed_at DESC,source_key LIMIT ? OFFSET ?',args+(limit,offset)).fetchall()
    output=[]
    for raw in rows:
        row=dict(raw)
        row.pop('rebuild_target',None); row.pop('rebuild_base',None)
        try:
            actual=row['byte_offset'] if row['state']=='imported_snapshot' else os.path.getsize(row['path'])
            row['backlog_bytes']=max(0,actual-row['byte_offset'])
            if actual<row['byte_offset']:
                row['state']='source_changed'
            elif row['backlog_bytes'] and row['state']=='complete':
                row['state']='backlog'   # 'rebuilding' and 'partial_record' keep their label
        except OSError:
            row['state']='source_missing'; row['backlog_bytes']=None
        row['blocks']=conn.execute('SELECT count(*) FROM memory_blocks WHERE source_key=?',(row['source_key'],)).fetchone()[0]
        row.pop('tail_hash',None); row.pop('tail_size',None); row.pop('head_hash',None); row.pop('generation',None)
        row['unsupported_types']=json.loads(row.get('unsupported_types') or '[]')
        row['skipped']={'excluded_by_policy':row.pop('excluded',0),'metadata_records':row.pop('metadata_records',0),
                        'unsupported':row.pop('omitted',0),'malformed':row.pop('malformed',0)}
        row['next_action']=next_action(row)
        output.append(row)
    chunks=conn.execute('SELECT count(*) FROM memory_chunks').fetchone()[0]
    vectors=conn.execute('SELECT count(*) FROM memory_vectors').fetchone()[0]
    return {'sources':output,'source_count':total,'source_agents':agents,'next_offset':offset+len(output) if offset+len(output)<total else None,
            'semantic':{'chunks':chunks,'vectors':vectors,'unembedded':chunks-vectors,'vector_format':'f32le-v1'},
            'legacy_sessions':conn.execute('SELECT count(*) FROM sessions').fetchone()[0],
            'coverage_notice':'Only explicitly indexed sources are searched. Complete means indexed to the checked file end, not that every session is registered. Source-agent counts cover the full selected scope, not freshness. Legacy capped exchanges require backfill.',
            'skipped_meaning':{'excluded_by_policy':'content deliberately not retained: tool results, reasoning, developer/system messages, image/thinking-only turns, mirrored events, Codex compaction summaries, Claude isMeta skill/command bodies',
                               'metadata_records':'records with no conversation content (titles, modes, attachments, usage)',
                               'unsupported':'shapes the adapter does not recognise; listed per source in unsupported_types',
                               'malformed':'lines that were not valid JSON'}}


def semantic_coverage(conn, repo_id=None, source_key=None):
    """Count passages/vectors in the retrieval scope, independently of its source page."""
    clauses, args = [], []
    for field, value in (('s.repo_id', repo_id), ('s.source_key', source_key)):
        if value:
            clauses.append(field+'=?'); args.append(value)
    counts = conn.execute('''SELECT count(*), count(v.chunk_id) FROM memory_chunks c
        JOIN memory_blocks b ON b.id=c.block_id
        JOIN memory_sources s ON s.source_key=b.source_key
        LEFT JOIN memory_vectors v ON v.chunk_id=c.id''' +
        (' WHERE '+' AND '.join(clauses) if clauses else ''), args).fetchone()
    return {'chunks': counts[0], 'vectors': counts[1], 'unembedded': counts[0]-counts[1],
            'vector_format': 'f32le-v1'}


def compact_coverage(status_result, repo_id=None, details=None):
    """Counts only: the per-source listing that search/brief carried (paths, states,
    actions) cost ~15 KB per call in Claude Code context; `status`/`sources` keep the detail."""
    rows = status_result.get('sources', [])
    states, skipped = {}, {}
    for row in rows:
        states[row['state']] = states.get(row['state'], 0) + 1
        for kind, count in row.get('skipped', {}).items():
            skipped[kind] = skipped.get(kind, 0) + count
    return {'repo_id': repo_id if repo_id is not None else status_result.get('repo_id'),
            'source_count': status_result['source_count'], 'checked_sources': len(rows),
            'source_agents': status_result.get('source_agents', {}),
            'next_offset': status_result.get('next_offset'), 'checked_source_states': states,
            'checked_backlog_bytes': sum((r.get('backlog_bytes') or 0) for r in rows),
            'checked_skipped_records': skipped, 'semantic': status_result.get('semantic'),
            'coverage_notice': status_result.get('coverage_notice'),
            'details': details or 'Latest source page checked only; run `status` (and `sources --offset N`) for source paths, freshness and repair actions.'}


def next_action(row):
    """One actionable instruction per source state (P08)."""
    key, state = row['source_key'], row['state']
    agent = row['agent']
    if state == 'imported_snapshot':
        return 'Explicit text snapshot; live freshness is unknown. Repeat history-import with a new export to refresh. No automatic account capture.'
    if state == 'source_missing':
        return 'Original transcript is gone; retained blocks stay readable. Nothing to do, or `prune %s` to drop them.' % key
    if state == 'source_changed':
        return 'Content before the saved cursor changed. Re-run `index %s --agent %s --rebuild` to replace this source.' % (row['path'], agent)
    if state in ('rebuilding','rebuild_ready'):
        return 'Rebuild in progress; earlier evidence stays searchable. Run `index %s --agent %s` (no --rebuild) to finish it.' % (row['path'], agent)
    if state == 'partial_record':
        return 'The writer is mid-record. Retry `index %s --agent %s` after the session writes more.' % (row['path'], agent)
    if state == 'backlog':
        return 'Unindexed bytes remain (%s). Run `index %s --agent %s` (repeat until complete) or let hooks catch up.' % (
            row.get('backlog_bytes'), row['path'], agent)
    if row['skipped']['unsupported']:
        return 'Complete, but %d record(s) had unrecognised shapes %s; report them so an adapter can be added.' % (
            row['skipped']['unsupported'], row['unsupported_types'])
    if row['skipped']['malformed']:
        return 'Complete; %d corrupt line(s) were skipped and cannot be recovered from this file.' % row['skipped']['malformed']
    return 'Complete and current.'


def git_state(cwd):
    """Read current repository state; commands never execute recalled instructions."""
    result={'cwd':str(Path(cwd).resolve()),'observed_at':datetime.now(timezone.utc).isoformat()}
    for name,args in [('branch',['branch','--show-current']),('commit',['rev-parse','HEAD']),('changes',['status','--short'])]:
        try:
            value=subprocess.check_output(['git','-C',cwd,*args],stderr=subprocess.DEVNULL,text=True,timeout=3).strip()
            result[name]=value[:4000]
        except (subprocess.SubprocessError,OSError):
            result[name]=None
    return result
