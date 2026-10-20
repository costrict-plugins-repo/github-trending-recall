"""P06/P09: safe interrupted rebuilds, edit detection, backup/restore/import."""
import json, sqlite3, sys
from pathlib import Path
import pytest
sys.path[:0]=[str(Path(__file__).parents[1]/'scripts')]
import memory_store as m
from db import get_connection, insert_session, prune_session
from recall_memory import parser, run


@pytest.fixture
def store(tmp_path):
    conn=get_connection(tmp_path/'m.db'); yield conn,tmp_path; conn.close()


def rec(role,text,key):
    return {'type':role,'uuid':key,'timestamp':'2026-09-05T12:00:00Z','message':{'role':role,'content':[{'type':'text','text':text}]}}


def write(p,*entries,mode='w'):
    with p.open(mode) as f:
        for e in entries: f.write(json.dumps(e)+'\n')


def test_interrupted_rebuild_keeps_old_evidence_until_complete(store):
    c,tmp=store; p=tmp/'t.jsonl'
    write(p,rec('assistant','original alpha','a'),rec('assistant','original beta','b'))
    m.index_file(c,p,session_id='s',cwd=str(tmp)); c.commit()
    # the file is rewritten with different keys and content
    write(p,rec('assistant','new one','n1'),rec('assistant','new two','n2'),rec('assistant','new three','n3'))
    assert m.index_file(c,p,session_id='s',cwd=str(tmp))['state']=='source_changed'
    r=m.index_file(c,p,session_id='s',cwd=str(tmp),rebuild=True,max_records=1); c.commit()
    assert r['state']=='rebuilding' and r['stale_removed']==0
    assert m.search(c,'original') and not m.search(c,'new')      # published generation remains complete
    assert 'Rebuild in progress' in m.status(c)['sources'][0]['next_action']
    while r['state']=='rebuilding':
        r=m.index_file(c,p,session_id='s',cwd=str(tmp),max_records=1); c.commit()
    assert r['state']=='complete' and r['stale_removed']==2
    assert not m.search(c,'original') and len(m.search(c,'new',limit=10))==3


def test_rebuild_of_unchanged_file_keeps_ids_and_removes_nothing(store):
    c,tmp=store; p=tmp/'t.jsonl'; write(p,rec('assistant','stable','a'))
    m.index_file(c,p,session_id='s',cwd=str(tmp)); c.commit()
    before=c.execute('SELECT id FROM memory_blocks').fetchone()[0]
    r=m.index_file(c,p,session_id='s',cwd=str(tmp),rebuild=True); c.commit()
    assert r['state']=='complete' and r['stale_removed']==0
    assert c.execute('SELECT id FROM memory_blocks').fetchone()[0]==before


def test_header_edit_is_detected_and_named(store):
    c,tmp=store; p=tmp/'t.jsonl'
    write(p,*[rec('assistant','filler '*20,str(i)) for i in range(6)])
    m.index_file(c,p,session_id='s',cwd=str(tmp)); c.commit()
    data=p.read_bytes(); p.write_bytes(b'{"type":"assistant","uuid":"X"'+data[len(b'{"type":"assistant","uuid":"0"'):])  # edit inside the first 256 bytes, same size
    r=m.index_file(c,p,session_id='s',cwd=str(tmp))
    assert r['state']=='source_changed' and 'header' in r['changed']
    assert 'header' in c.execute('SELECT error FROM memory_sources').fetchone()[0]


def test_backup_restore_round_trip(store):
    c,tmp=store; p=tmp/'t.jsonl'; write(p,rec('assistant','backed up evidence','a'))
    m.index_file(c,p,session_id='s',cwd=str(tmp)); c.commit()
    insert_session(c,'legacy','/p','h','2026-01-01'); c.commit()
    out=run(parser().parse_args(['backup',str(tmp/'b.db')]),c)
    assert out['integrity_check']=='ok'
    with pytest.raises(ValueError): run(parser().parse_args(['backup',str(tmp/'b.db')]),c)   # never overwrite
    c.execute('DELETE FROM memory_sources'); c.execute('DELETE FROM sessions'); c.commit()
    assert not m.search(c,'backed')
    with pytest.raises(ValueError): run(parser().parse_args(['restore',str(tmp/'b.db')]),c)  # needs --yes
    out=run(parser().parse_args(['restore',str(tmp/'b.db'),'--yes']),c)
    assert out['integrity_check']=='ok' and out['legacy_sessions']==1
    assert m.search(c,'backed')[0]['text']=='backed up evidence'


def test_export_prune_import_round_trip(store):
    c,tmp=store; p=tmp/'t.jsonl'
    write(p,rec('assistant','exportable '*300+'END','a'),rec('user','question','q'))
    m.index_file(c,p,session_id='s',cwd=str(tmp)); c.commit()
    export=run(parser().parse_args(['export','claude:s']),c)
    (tmp/'x.json').write_text(json.dumps(export))
    run(parser().parse_args(['prune','claude:s']),c)
    assert not m.search(c,'exportable') and c.execute('SELECT count(*) FROM memory_vectors').fetchone()[0]==0
    out=run(parser().parse_args(['import-export',str(tmp/'x.json')]),c)
    assert out['blocks_loaded']==2
    hit=m.search(c,'END')[0]
    assert m.get_block(c,hit['block_id'],max_chars=40000)['text'].endswith('END')
    assert m.status(c)['sources'][0]['state']=='source_missing'


def test_legacy_prune_removes_durable_but_durable_prune_keeps_legacy(store):
    c,tmp=store; p=tmp/'t.jsonl'; write(p,rec('assistant','shared','a'))
    m.index_file(c,p,session_id='s',cwd=str(tmp)); insert_session(c,'s','/p','h','2026-01-01'); c.commit()
    run(parser().parse_args(['prune','claude:s']),c)
    assert c.execute("SELECT count(*) FROM sessions WHERE session_id='s'").fetchone()[0]==1
    m.index_file(c,p,session_id='s',cwd=str(tmp)); c.commit()
    prune_session(c,'s')
    assert c.execute('SELECT count(*) FROM memory_sources').fetchone()[0]==0


def test_restore_of_older_schema_backup_migrates_forward(tmp_path):
    """A backup taken by v2.4 (schema 5, no durable tables) restores and is migrated to current."""
    from db import _SCHEMA_SQL, SCHEMA_VERSION, insert_session
    old=tmp_path/'v5-backup.db'
    c=sqlite3.connect(old); c.executescript(_SCHEMA_SQL); c.execute('PRAGMA user_version=5')
    c.execute("INSERT INTO sessions(session_id,project_path,project_hash,started_at) VALUES('legacy','/p','h','2026-01-01')"); c.commit(); c.close()
    conn=get_connection(tmp_path/'target.db')
    p=tmp_path/'t.jsonl'; write(p,rec('assistant','will be replaced','a')); m.index_file(conn,p,session_id='s',cwd=str(tmp_path)); conn.commit()
    out=run(parser().parse_args(['restore',str(old),'--yes']),conn)
    assert out['schema_version']==SCHEMA_VERSION and out['integrity_check']=='ok'
    assert out['legacy_sessions']==1 and out['sources']==0            # the backup's content, not the pre-restore content
    assert m.status(conn)['source_count']==0                          # durable tables exist again (empty)
    conn.close()
