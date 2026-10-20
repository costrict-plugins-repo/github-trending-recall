"""Retained text revisions and unpublished rebuilds. No model or network calls."""
import hashlib
import json
import time

FIELDS = ('id','source_key','message_key','seq','role','kind','timestamp','text',
          'start_byte','end_byte','content_hash','ordinal','generation')
COLUMNS = ','.join(FIELDS)
DECLARATIONS = '''id TEXT NOT NULL, source_key TEXT NOT NULL REFERENCES memory_sources(source_key) ON DELETE CASCADE,
    message_key TEXT NOT NULL, seq INTEGER NOT NULL, role TEXT NOT NULL, kind TEXT NOT NULL,
    timestamp TEXT NOT NULL, text TEXT NOT NULL, start_byte INTEGER NOT NULL, end_byte INTEGER NOT NULL,
    content_hash TEXT NOT NULL, ordinal INTEGER NOT NULL, generation INTEGER NOT NULL'''


def initialize(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS memory_revisions (revision_order INTEGER PRIMARY KEY, '+DECLARATIONS+
                 ',retired_at INTEGER NOT NULL,pinned INTEGER NOT NULL DEFAULT 0 CHECK(pinned IN (0,1)),UNIQUE(id,content_hash))')
    conn.execute('CREATE INDEX IF NOT EXISTS memory_revisions_retention ON memory_revisions(id,pinned,retired_at DESC,revision_order DESC)')
    conn.execute('CREATE INDEX IF NOT EXISTS memory_revisions_source ON memory_revisions(source_key)')
    conn.execute("CREATE TABLE IF NOT EXISTS memory_revision_policy (id INTEGER PRIMARY KEY CHECK(id=1),keep_last INTEGER NOT NULL CHECK(typeof(keep_last)='integer' AND keep_last>=0))")
    conn.execute('INSERT OR IGNORE INTO memory_revision_policy VALUES(1,3)')
    conn.execute('CREATE TABLE IF NOT EXISTS memory_rebuild_blocks ('+DECLARATIONS+',PRIMARY KEY(id))')
    conn.execute('CREATE INDEX IF NOT EXISTS memory_rebuild_message ON memory_rebuild_blocks(source_key,message_key)')
    conn.execute('CREATE INDEX IF NOT EXISTS memory_rebuild_sequence ON memory_rebuild_blocks(source_key,seq,ordinal)')
    conn.execute('''CREATE TABLE IF NOT EXISTS memory_rebuild_segments (
        source_key TEXT NOT NULL REFERENCES memory_sources(source_key) ON DELETE CASCADE,
        start_byte INTEGER NOT NULL,end_byte INTEGER NOT NULL,hash TEXT NOT NULL,
        PRIMARY KEY(source_key,start_byte))''')
    conn.execute('''CREATE TRIGGER IF NOT EXISTS memory_revisions_immutable BEFORE UPDATE OF '''+COLUMNS+''' ON memory_revisions
        BEGIN SELECT RAISE(ABORT,'Retained revision content is immutable'); END''')
    columns = {r[1] for r in conn.execute('PRAGMA table_info(memory_sources)')}
    for name, ddl in [('rebuild_in_progress','INTEGER NOT NULL DEFAULT 0'),
                      ('rebuild_target','TEXT'),('rebuild_base','TEXT')]:
        if name not in columns:
            conn.execute('ALTER TABLE memory_sources ADD COLUMN '+name+' '+ddl)
    # Schema 9 may already contain a mixed, interrupted rebuild. Preserve exactly
    # what is available, but require a fresh scan; old revisions cannot be invented.
    conn.execute("UPDATE memory_sources SET state='source_changed',error='Pre-revision rebuild interrupted; start an explicit rebuild to obtain an atomic replacement.' WHERE state='rebuilding' AND rebuild_in_progress=0")


def collect(conn, block_id=None, keep=None):
    if keep is None:
        keep = conn.execute('SELECT keep_last FROM memory_revision_policy WHERE id=1').fetchone()[0]
    if isinstance(keep,bool) or not isinstance(keep,int) or keep<0:
        raise ValueError('keep must be a nonnegative integer')
    ids = [block_id] if block_id is not None else [r[0] for r in conn.execute('SELECT DISTINCT id FROM memory_revisions')]
    deleted = 0
    for bid in ids:
        deleted += conn.execute('''DELETE FROM memory_revisions WHERE revision_order IN
            (SELECT revision_order FROM memory_revisions WHERE id=? AND pinned=0
             ORDER BY retired_at DESC,revision_order DESC LIMIT -1 OFFSET ?)''',(bid,keep)).rowcount
    return deleted


def remember(conn, block, pinned=False, trim=True):
    conn.execute('INSERT INTO memory_revisions ('+COLUMNS+',retired_at,pinned) VALUES ('+','.join('?' for _ in range(15))+''')
        ON CONFLICT(id,content_hash) DO UPDATE SET retired_at=excluded.retired_at,pinned=MAX(memory_revisions.pinned,excluded.pinned)''',
        tuple(block[k] for k in FIELDS)+(time.time_ns(),int(pinned)))
    if trim:
        collect(conn,block['id'])


def revision(conn, block_id, content_hash):
    # Prefer the retained snapshot if a text version has recurred in the live file.
    row = conn.execute('SELECT '+COLUMNS+' FROM memory_revisions WHERE id=? AND content_hash=?',(block_id,content_hash)).fetchone()
    if row is None:
        row=conn.execute('SELECT '+COLUMNS+' FROM memory_blocks WHERE id=? AND content_hash=?',(block_id,content_hash)).fetchone()
    return row


def pin(conn, block_id, content_hash, enabled=True):
    row=revision(conn,block_id,content_hash)
    if row is None:
        raise ValueError('Revision unavailable: expired, pruned, or never retained')
    if enabled:
        remember(conn,row,pinned=True)
    else:
        conn.execute('UPDATE memory_revisions SET pinned=0 WHERE id=? AND content_hash=?',(block_id,content_hash))
        collect(conn,block_id)
    return {'block_id':block_id,'revision':content_hash,'pinned':enabled}


def hash_range(stream, start, end):
    stream.seek(start); remaining=end-start; h=hashlib.sha256()
    while remaining:
        chunk=stream.read(min(1024*1024,remaining))
        if not chunk:
            raise ValueError('Rebuild source shrank before publication; restart with --rebuild')
        h.update(chunk);remaining-=len(chunk)
    return h.hexdigest()


def _publish(conn, source_key, path, offset, put_block):
    """Caller transaction publishes all changed blocks/chunks together.

    Deliberately a maintenance operation: hooks stage but never perform this
    potentially large transaction. Unchanged blocks keep their passages/vectors.
    """
    end=0
    with open(path,'rb') as stream:
        for segment in conn.execute('SELECT * FROM memory_rebuild_segments WHERE source_key=? ORDER BY start_byte',(source_key,)):
            if segment['start_byte']!=end or hash_range(stream,end,segment['end_byte'])!=segment['hash']:
                raise ValueError('Rebuild source changed within the scanned prefix; restart with --rebuild')
            end=segment['end_byte']
    if end!=offset:
        raise ValueError('Rebuild prefix is incomplete; restart with --rebuild')
    stale=0
    # Fetch only ids here; avoid collecting an entire retained transcript in RAM.
    removed=[r[0] for r in conn.execute('''SELECT b.id FROM memory_blocks b
        WHERE b.source_key=? AND NOT EXISTS (SELECT 1 FROM memory_rebuild_blocks n WHERE n.id=b.id)''',(source_key,))]
    for bid in removed:
        old=conn.execute('SELECT * FROM memory_blocks WHERE id=?',(bid,)).fetchone()
        remember(conn,old);conn.execute('DELETE FROM memory_blocks WHERE id=?',(bid,));stale+=1
    for row in conn.execute('SELECT * FROM memory_rebuild_blocks WHERE source_key=? ORDER BY seq,ordinal,id',(source_key,)):
        put_block(conn,dict(row))
    conn.execute('DELETE FROM memory_rebuild_blocks WHERE source_key=?',(source_key,))
    conn.execute('DELETE FROM memory_rebuild_segments WHERE source_key=?',(source_key,))
    conn.execute('UPDATE memory_sources SET rebuild_in_progress=0,rebuild_target=NULL,rebuild_base=NULL WHERE source_key=?',(source_key,))
    return stale


def published_source(source):
    row=dict(source)
    if row.get('rebuild_in_progress') and row.get('rebuild_base'):
        row=json.loads(row['rebuild_base'])
    for field in ('rebuild_target','rebuild_base'):
        row.pop(field,None)
    row['rebuild_in_progress']=0
    return row


def publish(conn, source_key, path, offset, put_block):
    conn.execute('SAVEPOINT recall_publish')
    try:
        result=_publish(conn,source_key,path,offset,put_block)
        conn.execute('RELEASE recall_publish')
        return result
    except BaseException:
        conn.execute('ROLLBACK TO recall_publish')
        conn.execute('RELEASE recall_publish')
        raise


def verify_content(conn):
    """Maintenance-only validation: never silently repair archived evidence."""
    problems=[]
    policy=conn.execute('SELECT keep_last FROM memory_revision_policy WHERE id=1').fetchone()
    if policy is None or type(policy[0]) is not int or policy[0]<0:
        problems.append('invalid revision retention policy')
    for table in ('memory_revisions','memory_rebuild_blocks'):
        for row in conn.execute('SELECT * FROM '+table):
            expected=hashlib.sha256(f"{row['source_key']}:{row['message_key']}:{row['ordinal']}:{row['kind']}".encode()).hexdigest()[:32]
            if row['id']!=expected or hashlib.sha256(row['text'].encode()).hexdigest()!=row['content_hash']:
                problems.append(table+' contains invalid identity or content hash');break
    if conn.execute('SELECT 1 FROM memory_revisions WHERE pinned NOT IN (0,1) LIMIT 1').fetchone():
        problems.append('invalid revision pin')
    return problems
