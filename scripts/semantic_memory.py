"""Optional, offline sentence-transformers embeddings. Never loaded by hooks.

Users supply an existing local model. The default installation/search stays stdlib.
The model's files are fingerprinted, so vectors cannot be queried by another model.
"""
import hashlib
import json
import math
import sqlite3
import struct
from functools import lru_cache
from pathlib import Path

VECTOR_FORMAT = 'f32le-v1'   # packed little-endian float32, dimension = len(blob) / 4


FLOAT32_MAX = 3.4028234663852886e38


def pack_vector(values):
    """float32 little-endian blob. Accepts only a non-empty JSON-style array of
    real numbers that are finite and representable in float32 (R3-02: a string
    or an object must not be reinterpreted as a vector, and 1e100 must be
    rejected rather than raise OverflowError inside a migration)."""
    if isinstance(values, (str, bytes, bytearray, dict)) or not isinstance(values, (list, tuple)):
        raise ValueError('vector must be an array of numbers')
    floats = []
    for v in values:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError('vector component is not a number')
        f = float(v)
        if not math.isfinite(f) or abs(f) > FLOAT32_MAX:
            raise ValueError('vector component is not a finite float32')
        floats.append(f)
    if not floats:
        raise ValueError('vector must be non-empty')
    return struct.pack('<%df' % len(floats), *floats)


def unpack_vector(stored):
    """Inverse of pack_vector; also reads pre-v9 JSON text rows."""
    if isinstance(stored, str):
        return tuple(float(v) for v in json.loads(stored))
    blob = bytes(stored)
    if not blob or len(blob) % 4:
        raise ValueError('corrupt vector blob (length %d)' % len(blob))
    return struct.unpack('<%df' % (len(blob) // 4), blob)


def model_identity(path):
    path=Path(path).expanduser().resolve()
    if not path.is_dir():
        raise ValueError('Supply an existing local model directory; no model is downloaded automatically.')
    files=sorted(p for p in path.rglob('*') if p.is_file() and p.suffix in ('.json','.txt','.bin','.safetensors'))
    if not files:
        raise ValueError('Model directory contains no model files')
    signature=tuple((str(p.relative_to(path)),p.stat().st_size,p.stat().st_mtime_ns) for p in files)
    return str(path),_fingerprint(str(path),signature)


@lru_cache(maxsize=4)
def _fingerprint(path,signature):
    h=hashlib.sha256()
    for relative,_,_ in signature:
        h.update(relative.encode())
        with open(Path(path)/relative,'rb') as file:
            for block in iter(lambda:file.read(1024*1024),b''):
                h.update(block)
    return h.hexdigest()


@lru_cache(maxsize=1)
def _load(path,identity):
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise RuntimeError('Optional semantic search needs sentence-transformers; lexical search remains available.') from exc
    return SentenceTransformer(path,local_files_only=True,trust_remote_code=False,device='cpu')


def encode(path,identity,texts):
    return _load(path,identity).encode(texts,normalize_embeddings=True,show_progress_bar=False).tolist()


def build(conn,model_path,batch_size=32):
    if batch_size<1 or batch_size>512:
        raise ValueError('batch-size must be between 1 and 512')
    path,identity=model_identity(model_path)
    conn.execute('CREATE TABLE IF NOT EXISTS memory_semantic_config (id INTEGER PRIMARY KEY CHECK(id=1),path TEXT,model TEXT)')
    if 'format' not in {r[1] for r in conn.execute('PRAGMA table_info(memory_semantic_config)')}:
        conn.execute('ALTER TABLE memory_semantic_config ADD COLUMN format TEXT')
        conn.execute('ALTER TABLE memory_semantic_config ADD COLUMN dimension INTEGER')
    prior=conn.execute('SELECT model FROM memory_semantic_config WHERE id=1').fetchone()
    if prior and prior[0]!=identity:
        conn.execute('DELETE FROM memory_vectors')
        conn.execute('UPDATE memory_semantic_config SET dimension=NULL WHERE id=1')
    conn.execute('INSERT INTO memory_semantic_config(id,path,model,format) VALUES(1,?,?,?) ON CONFLICT(id) DO UPDATE SET path=excluded.path,model=excluded.model,format=excluded.format',(path,identity,VECTOR_FORMAT))
    conn.commit()
    dimension=conn.execute('SELECT dimension FROM memory_semantic_config WHERE id=1').fetchone()[0]
    rows=conn.execute('''SELECT c.id,c.text FROM memory_chunks c LEFT JOIN memory_vectors v
        ON c.id=v.chunk_id AND v.model=? WHERE v.chunk_id IS NULL ORDER BY c.id''',(identity,)).fetchall()
    indexed=0
    for start in range(0,len(rows),batch_size):
        batch=rows[start:start+batch_size]
        vectors=encode(path,identity,[r['text'] for r in batch])
        if len(vectors)!=len(batch):
            raise RuntimeError('Embedding backend returned the wrong number of vectors')
        # Capture may replace chunks while inference runs outside a transaction.
        # Only attach vectors to the exact text encoded, under the same model.
        if conn.execute('SELECT model FROM memory_semantic_config WHERE id=1').fetchone()[0]!=identity:
            raise RuntimeError('Another build changed the configured model; rerun semantic-build.')
        for row,vector in zip(batch,vectors):
            if dimension is None:
                dimension=len(vector)
                conn.execute('UPDATE memory_semantic_config SET dimension=? WHERE id=1',(dimension,))
            if len(vector)!=dimension:
                raise RuntimeError('Embedding backend returned a %d-dim vector for a %d-dim index'%(len(vector),dimension))
            cursor=conn.execute('''INSERT OR REPLACE INTO memory_vectors(chunk_id,model,vector)
                SELECT id,?,? FROM memory_chunks WHERE id=? AND text=?''',
                (identity,sqlite3.Binary(pack_vector(vector)),row['id'],row['text']))
            indexed+=cursor.rowcount
        conn.commit()
    if dimension is None:   # R3-03: everything was already embedded; record the dimension from the rows
        existing=conn.execute("SELECT length(vector)/4 FROM memory_vectors WHERE model=? AND typeof(vector)='blob' LIMIT 1",(identity,)).fetchone()
        if existing:
            dimension=existing[0]
            conn.execute('UPDATE memory_semantic_config SET dimension=? WHERE id=1',(dimension,)); conn.commit()
    return {'model_fingerprint':identity,'model_path':path,'new_vectors':indexed,'format':VECTOR_FORMAT,'dimension':dimension,
            'note':'Explicit --semantic enables retrieval. New passages require another semantic-build.'}


def hybrid_search(conn,query,lexical,limit=5,repo_id=None,source_key=None,since=None,until=None,kind='text'):
    table=conn.execute("SELECT 1 FROM sqlite_master WHERE name='memory_semantic_config'").fetchone()
    config=conn.execute('SELECT * FROM memory_semantic_config WHERE id=1').fetchone() if table else None
    if not config:
        raise ValueError('No local semantic index. Run semantic-build first or omit --semantic.')
    path,identity=model_identity(config['path'])
    if identity!=config['model']:
        raise ValueError('Model files changed. Run semantic-build before querying these vectors.')
    clauses,args=['v.model=?'],[identity]
    if kind:
        clauses.append('b.kind=?');args.append(kind)
    for field,value in [('s.repo_id',repo_id),('s.source_key',source_key)]:
        if value:
            clauses.append(field+'=?');args.append(value)
    if since:
        clauses.append('b.timestamp>=?');args.append(since)
    if until:
        clauses.append('b.timestamp<?');args.append(until)
    rows=[dict(r) for r in conn.execute('''SELECT v.vector,c.id AS chunk_id,c.block_id,c.text,c.start_char,c.end_char,
        b.seq,b.role,b.kind,b.timestamp,b.content_hash,s.agent,s.session_id,s.source_key,s.repo_id,s.project_path
        FROM memory_vectors v JOIN memory_chunks c ON c.id=v.chunk_id
        JOIN memory_blocks b ON b.id=c.block_id JOIN memory_sources s ON s.source_key=b.source_key WHERE '''+' AND '.join(clauses),args)]
    if not rows:
        return lexical[:limit]
    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError('Optional semantic search needs NumPy and sentence-transformers; omit --semantic for lexical search.') from exc
    vector=np.asarray(encode(path,identity,[query])[0],dtype=np.float32)
    blobs=[r.pop('vector') for r in rows]
    blobs=[b if isinstance(b,(bytes,bytearray)) else pack_vector(unpack_vector(b)) for b in blobs]   # pre-v9 rows
    width=len(vector)*4
    if any(len(b)!=width for b in blobs):
        raise ValueError('Vector dimension mismatch; rebuild the semantic index')
    # One bounded matrix straight from the packed bytes: no JSON, no float lists.
    matrix=np.frombuffer(b''.join(blobs),dtype='<f4').reshape(len(blobs),len(vector))
    similarities=matrix@vector
    order=np.argsort(-similarities)[:max(30,limit*5)]
    # Deduplicate semantic chunks before fusing with lexical block ranks.
    semantic=[]
    seen=set()
    for rank,index in enumerate(order):
        row=rows[int(index)]; key=row['block_id']
        if key in seen:
            continue
        seen.add(key)
        row['similarity']=float(similarities[index])
        row['snippet']=row['text'][:400]
        row['get']=f"get {key} --start {row['start_char']}"
        from memory_store import evidence_provenance
        row['provenance']=evidence_provenance(row['role'],row['kind'])
        semantic.append(row)
    from retrieval_ranking import fuse
    return fuse(lexical,semantic,limit)
