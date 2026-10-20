"""Durable evidence contracts, ingestion failure boundaries, and agent isolation."""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path[:0]=[str(Path(__file__).parents[1]/'scripts'),str(Path(__file__).parents[1]/'hooks')]
import memory_store as m
from db import get_connection, insert_session, prune_session
from recall_memory import parser, run


@pytest.fixture
def store(tmp_path):
    conn=get_connection(tmp_path/'memory.db')
    yield conn,tmp_path
    conn.close()


def claude(role,text,key='one',stamp='2026-09-05T12:00:00Z'):
    return {'type':role,'uuid':key,'timestamp':stamp,'message':{'role':role,'content':[{'type':'text','text':text}]}}


def write(path,*entries):
    with path.open('a') as f:
        for e in entries:
            f.write(json.dumps(e)+'\n')


def ingest(store,*entries,agent='claude',sid='s',name='trace.jsonl',**kw):
    conn,tmp=store
    p=tmp/name
    write(p,*entries)
    result=m.index_file(conn,p,agent=agent,session_id=sid,cwd=str(tmp),**kw)
    conn.commit()
    return p,result


def test_full_answer_and_precise_pagination(store):
    c,_=store
    text='intermediate '*1400+'UNIQUE_FINAL_CONCLUSION'
    ingest(store,claude('assistant',text))
    hit=m.search(c,'UNIQUE_FINAL_CONCLUSION')[0]
    assert hit['start_char']>4000
    a=m.get_block(c,hit['block_id'],max_chars=7000)
    b=m.get_block(c,hit['block_id'],start=a['next_start'],max_chars=40000)
    assert a['text']+b['text']==text
    assert b['next_start'] is None
    assert m.search(c,'UNIQUE_FINAL_CONCLUSION require-all-absent',require_all=True)==[]


def test_stable_ids_and_no_duplicate_ingest(store):
    c,tmp=store
    p,_=ingest(store,claude('assistant','Stable evidence'))
    before=m.search(c,'evidence')[0]['block_id']
    assert m.index_file(c,p,session_id='s',cwd=str(tmp))['blocks']==0
    m.index_file(c,p,session_id='s',cwd=str(tmp),rebuild=True)
    assert m.search(c,'evidence')[0]['block_id']==before
    assert c.execute('SELECT count(*) FROM memory_blocks').fetchone()[0]==1


def test_partial_line_and_utf8_resume(store):
    c,tmp=store;p=tmp/'partial.jsonl'
    raw=(json.dumps(claude('user','café question'),ensure_ascii=False)+'\n').encode()
    at=raw.index('é'.encode())+1
    p.write_bytes(raw[:at])
    assert m.index_file(c,p,session_id='s',cwd=str(tmp))['offset']==0
    p.write_bytes(raw)
    assert m.index_file(c,p,session_id='s',cwd=str(tmp))['state']=='complete'
    assert m.search(c,'café')


def test_valid_json_without_terminator_stays_pending(store):
    c,tmp=store;p=tmp/'partial.jsonl';p.write_text(json.dumps(claude('user','question')))
    assert m.index_file(c,p,session_id='s',cwd=str(tmp))['state']=='partial_record'
    assert c.execute('SELECT count(*) FROM memory_blocks').fetchone()[0]==0


def test_caps_make_progress_through_large_turn(store):
    c,tmp=store;p=tmp/'long.jsonl'
    write(p,claude('user','task','q'),*[claude('assistant','evidence '*500,str(i)) for i in range(10)])
    offsets=[]
    for _ in range(20):
        r=m.index_file(c,p,session_id='s',cwd=str(tmp),max_bytes=1000,max_records=2)
        offsets.append(r['offset'])
        if r['state']=='complete':break
    assert offsets==sorted(set(offsets))
    assert offsets[-1]==p.stat().st_size
    assert c.execute('SELECT count(*) FROM memory_blocks').fetchone()[0]==11


def test_modified_source_preserves_history_until_explicit_rebuild(store):
    c,tmp=store;p,_=ingest(store,claude('assistant','Original evidence'))
    p.write_text(json.dumps(claude('assistant','Different evidence'))+'\n')
    assert m.index_file(c,p,session_id='s',cwd=str(tmp))['state']=='source_changed'
    assert m.search(c,'Original')
    m.index_file(c,p,session_id='s',cwd=str(tmp),rebuild=True)
    assert not m.search(c,'Original')
    assert m.search(c,'Different')


def test_codex_response_items_only_and_no_internal_instructions(store):
    c,_=store
    entries=[{'type':'session_meta','payload':{'id':'codex-session','cwd':'/project'}},
             {'type':'event_msg','payload':{'type':'user_message','message':'mirrored duplicate'}},
             {'type':'response_item','payload':{'type':'message','id':'u','role':'user','content':[{'type':'input_text','text':'Question here'}]}},
             {'type':'response_item','payload':{'type':'message','id':'d','role':'developer','content':[{'type':'text','text':'INTERNAL_DIRECTIVE'}]}},
             {'type':'response_item','payload':{'type':'message','id':'reason','role':'assistant','channel':'analysis','content':[{'type':'text','text':'PRIVATE_REASONING'}]}},
             {'type':'response_item','payload':{'type':'message','id':'a','role':'assistant','content':[{'type':'output_text','text':'The corrected answer'}]}},
             {'type':'response_item','payload':{'type':'function_call','call_id':'t','name':'exec_command','arguments':'{"cmd":"pytest -q"}'}},
             {'type':'response_item','payload':{'type':'function_call_output','call_id':'t','output':'EXCLUDED_TOOL_RESULT'}}]
    ingest(store,*entries,agent='codex')
    assert c.execute('SELECT count(*) FROM memory_blocks').fetchone()[0]==3
    assert m.search(c,'pytest',kind='tool_use')[0]['agent']=='codex'
    for query in ('INTERNAL_DIRECTIVE','PRIVATE_REASONING','EXCLUDED_TOOL_RESULT','mirrored'):
        assert not m.search(c,query)


def test_agent_source_ids_do_not_collide(store):
    c,_=store
    ingest(store,claude('assistant','Claude evidence'),sid='same')
    ingest(store,{'type':'message','id':'one','role':'assistant','content':[{'type':'text','text':'Codex evidence'}]},
           agent='codex',sid='same',name='codex.jsonl')
    assert len(m.search(c,'evidence'))==2
    assert len(m.search(c,'evidence',source_key='codex:same'))==1


def test_redaction_before_chunks_and_fts(store):
    c,_=store
    ingest(store,claude('assistant','x '*780+'my password is fakepassword123'))
    assert not m.search(c,'fakepassword123')
    assert '[REDACTED' in c.execute('SELECT text FROM memory_blocks').fetchone()[0]


def test_prune_cascades_chunks_and_fts(store):
    c,_=store
    ingest(store,claude('assistant','deletable evidence'))
    insert_session(c,'s','/p','h','2026-01-01')
    prune_session(c,'s')
    assert not m.search(c,'deletable')
    assert c.execute('SELECT count(*) FROM memory_chunks').fetchone()[0]==0
    c.execute("INSERT INTO memory_fts(memory_fts,rank) VALUES('integrity-check',1)")


def test_missing_source_still_has_readable_evidence(store):
    c,_=store;p,_=ingest(store,claude('assistant','preserved evidence'))
    p.unlink()
    assert m.status(c)['sources'][0]['state']=='source_missing'
    hit=m.search(c,'preserved')[0]
    assert m.get_block(c,hit['block_id'])['text']=='preserved evidence'


def test_changed_model_invalidates_vectors_on_rebuild(store,monkeypatch):
    pytest.importorskip('numpy')
    import semantic_memory as sem
    c,tmp=store;ingest(store,claude('assistant','semantic evidence'))
    model=tmp/'model';model.mkdir();(model/'config.json').write_text('{}')
    monkeypatch.setattr(sem,'encode',lambda *args:[[1.0,0.0] for _ in args[-1]])
    sem.build(c,model)
    (model/'config.json').write_text('{"changed":true}')
    with pytest.raises(ValueError,match='changed'):
        sem.hybrid_search(c,'query',[])
    sem.build(c,model)
    assert sem.hybrid_search(c,'query',[])[0]['text']=='semantic evidence'


def test_brief_preserves_opening_and_final_tail_as_evidence(store):
    c,_=store
    ingest(store,claude('user','Original objective','q'),claude('assistant','notes '*1000+'Finally rejected batching.','a'))
    result=m.brief(c)
    assert result['sampled']
    assert len(result['evidence'])==2
    assert result['evidence'][1]['excerpts'][-1]['text'].endswith('Finally rejected batching.')
    assert result['evidence'][1]['excerpts'][-1]['start_char']>0


def test_cli_empty_search_reports_coverage(store):
    c,_=store
    args=parser().parse_args(['search','no results','--all'])
    result=run(args,c)
    assert result['hits']==[]
    assert 'coverage' in result


def test_source_metadata_updates_when_backlog_grows(store):
    c,_=store;p,_=ingest(store,claude('assistant','A'))
    write(p,claude('assistant','B','two'))
    state=m.status(c)['sources'][0]
    assert state['state']=='backlog'
    assert state['backlog_bytes']>0


def test_repo_normalizes_remote_protocols(monkeypatch):
    monkeypatch.setattr(m.subprocess,'check_output',lambda *a,**k:'git@github.com:owner/project.git\n')
    ssh=m.repository_identity('/tmp/a')
    monkeypatch.setattr(m.subprocess,'check_output',lambda *a,**k:'https://credential@github.com/owner/project.git\n')
    assert m.repository_identity('/tmp/b')==ssh=='github.com/owner/project'


def test_v5_upgrade_preserves_legacy_rows_and_search(tmp_path):
    from db import _SCHEMA_SQL, insert_exchanges
    path=tmp_path/'v5.db'
    c=sqlite3.connect(path)
    c.row_factory=sqlite3.Row
    c.executescript(_SCHEMA_SQL)
    c.execute('PRAGMA user_version=5')
    insert_session(c,'old','/project','hash','2026-01-01')
    insert_exchanges(c,'old',[dict(idx=1,timestamp='2026-01-01',preview='legacy',
                                  user_text='question',assistant_text='migration sentinel')])
    c.close()
    c=get_connection(path)
    try:
        from db import SCHEMA_VERSION
        assert c.execute('PRAGMA user_version').fetchone()[0]==SCHEMA_VERSION
        assert c.execute("SELECT assistant_text FROM exchanges_fts WHERE exchanges_fts MATCH 'sentinel'").fetchone()[0]=='migration sentinel'
        assert m.status(c)['legacy_sessions']==1
        assert m.status(c)['source_count']==0
        c.execute("INSERT INTO memory_fts(memory_fts,rank) VALUES('integrity-check',1)")
    finally:
        c.close()


def test_session_end_drains_durable_backfill_when_legacy_already_at_eof(store):
    from db import update_session_offset
    from session_end import run_hook
    c,tmp=store
    path=tmp/'backfill.jsonl'
    write(path,*[claude('assistant','backfill evidence '+str(i),str(i)) for i in range(1005)])
    insert_session(c,'s',str(tmp),'hash','2026-01-01',transcript_path=str(path))
    update_session_offset(c,'s',path.stat().st_size,0)
    run_hook({'session_id':'s','transcript_path':str(path)},db_path=tmp/'memory.db')
    assert c.execute('SELECT count(*) FROM memory_blocks').fetchone()[0]==1005
    assert m.status(c)['sources'][0]['state']=='complete'


def test_prose_and_command_searches_are_explicit(store):
    c,_=store
    ingest(store,claude('assistant','pytest outcome explained'),
           {'type':'assistant','uuid':'tool','message':{'role':'assistant','content':[
               {'type':'tool_use','name':'Bash','input':{'command':'pytest -q'}}]}})
    assert [r['kind'] for r in m.search(c,'What was the pytest outcome?')]==['text']
    assert [r['kind'] for r in m.search(c,'pytest',kind='tool_use')]==['tool_use']
    assert len(m.search(c,'pytest',kind=None))==2


def test_index_reports_committed_progress_when_budget_expires(store,monkeypatch):
    import recall_memory as cli
    c,tmp=store
    path=tmp/'budget.jsonl';write(path,claude('assistant','budget evidence'))
    ticks=iter([0,0,2,2])
    monkeypatch.setattr(cli.time,'monotonic',lambda:next(ticks))
    args=parser().parse_args(['index',str(path),'--agent','claude','--seconds','1'])
    result=run(args,c)
    assert result['files_processed']==1
    assert result['results'][0]['offset']==path.stat().st_size
    assert result['budget_exhausted']


def test_semantic_build_does_not_attach_stale_vector_after_capture(store,monkeypatch):
    import semantic_memory as sem
    c,tmp=store
    path,_=ingest(store,claude('assistant','original semantic evidence'))
    model=tmp/'model';model.mkdir();(model/'config.json').write_text('{}')
    def concurrent_encode(*args):
        path.write_text(json.dumps(claude('assistant','replacement semantic evidence'))+'\n')
        m.index_file(c,path,session_id='s',cwd=str(tmp),rebuild=True)
        return [[1.0,0.0]]
    monkeypatch.setattr(sem,'encode',concurrent_encode)
    assert sem.build(c,model)['new_vectors']==0
    assert c.execute('SELECT count(*) FROM memory_vectors').fetchone()[0]==0


def test_cli_reports_total_blocks_across_import_passes(store):
    c,tmp=store
    path=tmp/'multi-pass.jsonl'
    write(path,*[claude('assistant','retained evidence '+str(i),str(i)) for i in range(1001)])
    result=run(parser().parse_args(['index',str(path),'--agent','claude']),c)
    assert result['results'][0]['state']=='complete'
    assert result['results'][0]['blocks']==1001


# The exact v6 DDL from commit 87918cb (before ordinal / skipped-record columns).
_V6_SCHEMA = [
    """CREATE TABLE memory_sources (
        source_key TEXT PRIMARY KEY, session_id TEXT NOT NULL, agent TEXT NOT NULL,
        path TEXT NOT NULL, project_path TEXT NOT NULL, repo_id TEXT NOT NULL,
        byte_offset INTEGER NOT NULL DEFAULT 0, source_size INTEGER NOT NULL DEFAULT 0,
        last_indexed_at TEXT, state TEXT NOT NULL DEFAULT 'pending', error TEXT,
        omitted INTEGER NOT NULL DEFAULT 0, malformed INTEGER NOT NULL DEFAULT 0,
        tail_hash TEXT, tail_size INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE memory_blocks (
        id TEXT PRIMARY KEY, source_key TEXT NOT NULL REFERENCES memory_sources(source_key) ON DELETE CASCADE,
        message_key TEXT NOT NULL, seq INTEGER NOT NULL, role TEXT NOT NULL,
        kind TEXT NOT NULL, timestamp TEXT NOT NULL, text TEXT NOT NULL,
        start_byte INTEGER NOT NULL, end_byte INTEGER NOT NULL, content_hash TEXT NOT NULL)""",
    "CREATE INDEX memory_blocks_source ON memory_blocks(source_key, seq, start_byte)",
    """CREATE TABLE memory_chunks (
        id INTEGER PRIMARY KEY, block_id TEXT NOT NULL REFERENCES memory_blocks(id) ON DELETE CASCADE,
        ordinal INTEGER NOT NULL, start_char INTEGER NOT NULL, end_char INTEGER NOT NULL,
        text TEXT NOT NULL, UNIQUE(block_id, ordinal))""",
    """CREATE VIRTUAL TABLE memory_fts USING fts5(
        text, content=memory_chunks, content_rowid=id, tokenize='porter unicode61')""",
    """CREATE TRIGGER memory_chunks_insert AFTER INSERT ON memory_chunks BEGIN
        INSERT INTO memory_fts(rowid,text) VALUES(new.id,new.text); END""",
    """CREATE TRIGGER memory_chunks_delete AFTER DELETE ON memory_chunks BEGIN
        INSERT INTO memory_fts(memory_fts,rowid,text) VALUES('delete',old.id,old.text); END""",
    """CREATE TABLE memory_vectors (
        chunk_id INTEGER PRIMARY KEY REFERENCES memory_chunks(id) ON DELETE CASCADE,
        model TEXT NOT NULL, vector TEXT NOT NULL)""",
]


def test_v6_development_store_upgrades_to_current(tmp_path):
    """A store created by the first durable-memory revision (schema 6) gains the
    v7 columns, keeps its rows, and still passes FTS integrity."""
    from db import _SCHEMA_SQL, SCHEMA_VERSION
    path=tmp_path/'v6.db'
    c=sqlite3.connect(path); c.executescript(_SCHEMA_SQL)
    for ddl in _V6_SCHEMA: c.execute(ddl)
    c.execute("INSERT INTO memory_sources(source_key,session_id,agent,path,project_path,repo_id) VALUES('claude:s','s','claude','/t','/p','r')")
    c.execute("INSERT INTO memory_blocks VALUES('b1','claude:s','k',1,'assistant','text','2026-01-01','v6 evidence',0,10,'h')")
    c.execute("INSERT INTO memory_chunks(block_id,ordinal,start_char,end_char,text) VALUES('b1',0,0,11,'v6 evidence')")
    c.execute('PRAGMA user_version=6'); c.commit(); c.close()
    c=get_connection(path)
    try:
        assert c.execute('PRAGMA user_version').fetchone()[0]==SCHEMA_VERSION
        assert {'excluded','metadata_records','unsupported_types'} <= {r[1] for r in c.execute('PRAGMA table_info(memory_sources)')}
        assert 'ordinal' in {r[1] for r in c.execute('PRAGMA table_info(memory_blocks)')}
        assert m.search(c,'evidence')[0]['block_id']=='b1'
        assert m.status(c)['sources'][0]['skipped']['unsupported']==0
        c.execute("INSERT INTO memory_fts(memory_fts,rank) VALUES('integrity-check',1)")
    finally:
        c.close()
