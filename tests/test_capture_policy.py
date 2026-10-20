"""Policy enforcement, bypass paths, serialized updates and stale-backup recovery."""
import json,sqlite3,sys
from pathlib import Path
import pytest
sys.path[:0]=[str(Path(__file__).parents[1]/'scripts'),str(Path(__file__).parents[1]/'hooks')]
from db import get_connection,SCHEMA_VERSION
import recall_privacy as policy
import memory_store as memory
from recall_memory import parser,run
from recall_capture import CaptureWorker
from memory_transfer import export_source,import_source
from prompt_submit import index_transcript


def write(path,agent='codex',sid='s'):
    if agent=='codex':
        rows=[{'type':'session_meta','payload':{'id':sid,'cwd':str(path.parent)}},
              {'type':'response_item','payload':{'type':'message','id':'u','role':'user','content':[{'type':'input_text','text':'PRIVATE amber objective'}]}}]
    else:
        rows=[{'type':'user','sessionId':sid,'uuid':'u','message':{'role':'user','content':'PRIVATE amber objective'}},
              {'type':'assistant','sessionId':sid,'uuid':'a','message':{'role':'assistant','content':'PRIVATE amber accepted'}}]
    path.write_text(''.join(json.dumps(x)+'\n' for x in rows))
    return path


@pytest.fixture
def store(tmp_path):
    c=get_connection(tmp_path/'store.db')
    yield c,tmp_path
    c.close()


@pytest.mark.parametrize('agent',['claude','codex'])
def test_disabled_source_not_captured_even_with_alias_or_copy(store,agent):
    c,p=store;path=write(p/'s.jsonl',agent)
    policy.set_mode(c,agent+':s','off')
    copy=p/'copy.jsonl';copy.write_bytes(path.read_bytes())
    for source in (path,copy):
        r=memory.index_file(c,source,agent=agent,session_id='renamed')
        assert r['state']=='capture_disabled'
    assert not c.execute('SELECT * FROM memory_sources').fetchall()
    assert not memory.search(c,'PRIVATE')


def test_legacy_and_durable_hook_capture_disabled_before_any_retention(store):
    c,p=store;path=write(p/'s.jsonl','claude')
    policy.set_mode(c,'claude:s','off')
    assert index_transcript(c,'renamed',str(path))==[]
    for table in ('sessions','exchanges','tags','highlights','memory_sources','memory_blocks'):
        assert c.execute('SELECT count(*) FROM '+table).fetchone()[0]==0
    with pytest.raises(sqlite3.IntegrityError,match='capture disabled'):
        c.execute("INSERT INTO sessions(session_id,project_path,project_hash,started_at) VALUES('s','p','h','t')")


def test_off_keeps_existing_evidence_but_blocks_old_writer_appends(store):
    c,p=store;path=write(p/'s.jsonl','claude');index_transcript(c,'s',str(path));c.commit()
    policy.set_mode(c,'claude:s','off')
    assert memory.search(c,'amber')
    with pytest.raises(sqlite3.IntegrityError,match='capture disabled'):
        c.execute("UPDATE exchanges SET assistant_text='NEW SECRET' WHERE session_id='s'")
    assert c.execute("SELECT assistant_text FROM exchanges").fetchone()[0] != 'NEW SECRET'
    with pytest.raises(ValueError,match='backfill'):
        policy.set_mode(c,'claude:s','shared')
    assert policy.mode(c,'claude:s')=='off'
    policy.set_mode(c,'claude:s','shared',allow_backfill=True)
    assert policy.mode(c,'claude:s')=='shared'


def test_prune_retains_suppression_and_directory_worker_obeys_it(store):
    c,p=store;path=write(p/'s.jsonl')
    memory.index_file(c,path,agent='codex');c.commit();policy.set_mode(c,'codex:s','off')
    run(parser().parse_args(['prune','codex:s']),c)
    r=CaptureWorker(c,[path],'codex',cwd=p).refresh(1)
    assert not r['errors'] and next(iter(r['states'].values()))['state']=='capture_disabled'
    assert policy.mode(c,'codex:s')=='off' and not memory.search(c,'PRIVATE')


def test_import_preserves_off_and_cannot_relax_existing_suppression(store,tmp_path):
    c,p=store;path=write(p/'s.jsonl');memory.index_file(c,path,agent='codex');c.commit()
    policy.set_mode(c,'codex:s','off');data=export_source(c,'codex:s',True)
    other=get_connection(tmp_path/'other.db')
    import_source(other,data);other.commit()
    assert policy.mode(other,'codex:s')=='off' and memory.search(other,'amber')
    data['capture_policy']['mode']='shared'
    with pytest.raises(ValueError,match='capture disabled'):import_source(other,data)
    assert policy.mode(other,'codex:s')=='off';other.close()


def test_stale_restore_preserves_later_capture_decision(store):
    c,p=store;path=write(p/'s.jsonl');memory.index_file(c,path,agent='codex');c.commit()
    old=p/'old.db';run(parser().parse_args(['backup',str(old)]),c)
    policy.set_mode(c,'codex:s','off')
    run(parser().parse_args(['restore',str(old),'--yes']),c)
    assert policy.mode(c,'codex:s')=='off'
    assert memory.index_file(c,path,agent='codex')['state']=='capture_disabled'


def test_missing_policy_table_fails_before_capture(store):
    c,p=store;path=write(p/'s.jsonl');c.execute('DROP TABLE recall_capture_policy');c.commit()
    with pytest.raises(sqlite3.OperationalError):memory.index_file(c,path,agent='codex')
    assert c.execute('SELECT count(*) FROM memory_blocks').fetchone()[0]==0


def test_policy_update_waits_for_capture_transaction(store):
    c,p=store;other=get_connection(p/'store.db');other.execute('PRAGMA busy_timeout=10')
    c.execute('BEGIN IMMEDIATE')
    with pytest.raises(sqlite3.OperationalError,match='locked'):policy.set_mode(other,'codex:s','off')
    c.rollback();policy.set_mode(other,'codex:s','off');other.close()
    assert policy.mode(c,'codex:s')=='off'


def test_newer_schema_is_refused_by_writer(tmp_path):
    path=tmp_path/'future.db';c=get_connection(path);c.execute('PRAGMA user_version='+str(SCHEMA_VERSION+1));c.close()
    with pytest.raises(ValueError,match='newer'):get_connection(path)


@pytest.mark.parametrize('agent',['claude','codex'])
def test_late_or_missing_identity_cannot_evade_suppression(store,agent):
    c,p=store;path=write(p/'aliased.jsonl',agent)
    path.write_text('{}\n'*31+path.read_text())
    policy.set_mode(c,agent+':s','off')
    with pytest.raises(ValueError,match='Cannot establish transcript identity'):
        memory.index_file(c,path,agent=agent,session_id='different')
    if agent=='claude':
        with pytest.raises(ValueError,match='Cannot establish transcript identity'):
            index_transcript(c,'different',str(path))
    assert not memory.search(c,'PRIVATE')
