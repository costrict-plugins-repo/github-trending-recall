"""Backend isolation; these are not native-host authentication receipts."""
import json,sys,sqlite3,shutil
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
from db import get_connection,get_read_connection
from recall_private_store import SessionStore,SessionRecallService
from recall_mcp import RecallService
import memory_store as memory
import recall_privacy as policy


def trace(path,sid,text):
    path.write_text(json.dumps({'type':'session_meta','payload':{'id':sid,'cwd':str(path.parent)}})+'\n'+json.dumps({'type':'response_item','payload':{'type':'message','id':'u','role':'user','content':[{'type':'input_text','text':text}]}})+'\n')
    return path


@pytest.fixture
def private(tmp_path):
    shared=get_connection(tmp_path/'shared.db')
    store=SessionStore.create(shared,tmp_path/'private','codex:owner',tmp_path)
    store.capture(trace(tmp_path/'owner.jsonl','owner','exclusive violet-raven decision'))
    yield shared,store,tmp_path
    shared.close()


def test_owner_reads_and_shared_foreign_reader_cannot_find_private(private):
    shared,store,p=private
    hits=store.reader().call('recall_search',{'query':'violet-raven'})['hits'];assert hits
    assert not RecallService(p/'shared.db',store.repo_id).call('recall_search',{'query':'violet-raven'})['hits']
    assert memory.status(shared)['source_count']==0
    assert policy.mode(shared,'codex:owner')=='off'
    with pytest.raises(ValueError):SessionRecallService(store.path,store.repo_id,'codex:foreign').call('recall_get',{'block_id':hits[0]['block_id']})
    with pytest.raises(ValueError,match='Unknown or missing'):store.reader().call('recall_search',{'query':'violet','owner':'codex:foreign'})
    g=store.reader().call('recall_get',{'block_id':hits[0]['block_id'],'quote':'violet-raven'})
    assert g['citation_check']['valid']


@pytest.mark.parametrize('op',['cli-read','cli-write','mcp-read','copied-backup'])
def test_unbound_access_refused_even_with_explicit_database_path(private,op):
    _,store,p=private;path=store.path
    if op=='copied-backup':
        c=get_connection(path,private_owner=store.owner);path=p/'copy.db';dest=sqlite3.connect(path);c.backup(dest);dest.close();c.close()
        with pytest.raises(ValueError,match='bound private reader'):
            get_connection(path)  # Creates the lease, but still refuses unbound access.
    with pytest.raises(ValueError,match='bound private reader'):
        if op=='cli-write':get_connection(path)
        elif op=='mcp-read':RecallService(path,store.repo_id).call('recall_status',{})
        else:get_read_connection(path)


def test_foreign_capture_and_shared_recapture_refused(private):
    shared,store,p=private
    with pytest.raises(ValueError,match='identity mismatch'):store.capture(trace(p/'fork.jsonl','fork','foreign marker'))
    r=memory.index_file(shared,p/'owner.jsonl',agent='codex');assert r['state']=='capture_disabled'
    assert not memory.search(shared,'violet')


def test_existing_shared_content_cannot_be_silently_declared_private(tmp_path):
    c=get_connection(tmp_path/'shared.db');p=trace(tmp_path/'s.jsonl','s','already shared')
    memory.index_file(c,p,agent='codex');c.commit()
    with pytest.raises(ValueError,match='reviewed conversion'):SessionStore.create(c,tmp_path/'private','codex:s',tmp_path)
    assert policy.mode(c,'codex:s')=='shared' and memory.search(c,'shared')
    c.close()


def test_private_files_and_binding_missing_fail_closed(private):
    _,store,_=private
    assert store.path.stat().st_mode & 0o777 == 0o600
    c=get_connection(store.path,private_owner=store.owner);c.execute('DELETE FROM recall_private_owner');c.commit();c.close()
    with pytest.raises(ValueError,match='access refused'):store.reader().call('recall_status',{})


def test_damaged_private_marker_never_becomes_a_shared_store(private):
    _,store,_=private
    c=get_connection(store.path,private_owner=store.owner);c.execute('DROP TABLE recall_private_owner');c.commit();c.close()
    with pytest.raises(ValueError,match='binding is missing'):get_read_connection(store.path)


def test_private_route_cannot_be_relaxed_by_capture_policy(private):
    shared,store,p=private
    with pytest.raises(ValueError,match='private routing'):
        policy.set_mode(shared,store.owner,'shared',allow_backfill=True)
    assert policy.mode(shared,store.owner)=='off'


def test_damaged_private_capture_guard_is_rejected(private):
    _,store,_=private
    c=get_connection(store.path,private_owner=store.owner)
    c.execute('DROP TRIGGER recall_private_memory_sources_insert');c.commit();c.close()
    with pytest.raises(ValueError,match='guards are incomplete'):store.reader().call('recall_status',{})


def test_private_setup_failure_never_falls_back_to_shared(tmp_path,monkeypatch):
    import recall_private_store as backend
    c=get_connection(tmp_path/'shared.db')
    def fail(*args,**kwargs):raise OSError('simulated private-disk failure')
    monkeypatch.setattr(backend,'get_connection',fail)
    with pytest.raises(OSError):SessionStore.create(c,tmp_path/'private','codex:owner',tmp_path)
    assert policy.mode(c,'codex:owner')=='off'
    path=trace(tmp_path/'owner.jsonl','owner','never shared after failed setup')
    assert memory.index_file(c,path,agent='codex')['state']=='capture_disabled'
    assert not memory.search(c,'never shared');c.close()


def test_private_revision_and_neighbors_stay_owner_bound(private):
    _,store,p=private;reader=store.reader()
    first=reader.call('recall_search',{'query':'violet'})['hits'][0]
    with (p/'owner.jsonl').open('a') as f:
        for key,text in [('u','revised cobalt choice'),('neighbor','adjacent owner evidence')]:
            f.write(json.dumps({'type':'response_item','payload':{'type':'message','id':key,'role':'user','content':[{'type':'input_text','text':text}]}})+'\n')
    store.capture(p/'owner.jsonl')
    old=reader.call('recall_get',{'block_id':first['block_id'],'revision':first['content_hash'],'quote':'violet-raven'})
    assert old['citation_check']['valid']
    current=reader.call('recall_get',{'block_id':first['block_id'],'neighbors':1})
    assert current['text']=='revised cobalt choice' and current['neighbors']
    foreign=SessionRecallService(store.path,store.repo_id,'codex:foreign')
    with pytest.raises(ValueError):foreign.call('recall_get',{'block_id':first['block_id'],'revision':first['content_hash']})


def test_private_sql_guard_refuses_foreign_capture_in_bound_connection(private):
    _,store,p=private;c=get_connection(store.path,private_owner=store.owner)
    path=trace(p/'foreign.jsonl','foreign','unwanted foreign evidence')
    with pytest.raises(sqlite3.IntegrityError,match='Foreign source refused'):
        memory.index_file(c,path,agent='codex')
    c.rollback();assert not c.execute("SELECT 1 FROM memory_sources WHERE source_key='codex:foreign'").fetchone();c.close()
