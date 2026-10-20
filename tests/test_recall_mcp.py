"""Wire, scope and read-only contracts for the optional local MCP interface."""
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from db import get_connection
import memory_store as memory
from recall_mcp import MAX_MESSAGE, Protocol, RecallService, read_connection, serve


@pytest.fixture
def corpus(tmp_path):
    path = tmp_path/'store?# with space.db'
    conn = get_connection(path)
    labels = {}
    for agent, sid, repo, text in [
        ('claude','accepted','repo-a','Accepted decision: use amber-sparrow because it avoids the legacy label collision. ' + 'retained text '*900),
        ('codex','rejected','repo-a','Rejected decision: the violet delivery label collides with an existing identifier.'),
        ('codex','hidden','repo-b','Secret-other-project quasar result.')]:
        trace=tmp_path/(sid+'.jsonl')
        row={'type':'assistant','uuid':sid,'cwd':str(tmp_path),'sessionId':sid,'message':{'role':'assistant','content':text}} if agent=='claude' else {
             'type':'response_item','payload':{'id':sid,'type':'message','role':'assistant','content':[{'type':'output_text','text':text}]}}
        trace.write_text(json.dumps(row)+'\n')
        memory.index_file(conn,trace,agent=agent,session_id=sid,cwd=str(tmp_path)); conn.commit()
        conn.execute('UPDATE memory_sources SET repo_id=? WHERE source_key=?',(repo,f'{agent}:{sid}')); conn.commit()
        labels[sid]=conn.execute('SELECT id FROM memory_blocks WHERE source_key=?',(f'{agent}:{sid}',)).fetchone()[0]
    conn.close()
    return path, labels


def ready(service, version='2025-11-25'):
    protocol=Protocol(service)
    init=protocol.handle({'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':version,'capabilities':{},'clientInfo':{'name':'contract-test','version':'1'}}})
    protocol.handle({'jsonrpc':'2.0','method':'notifications/initialized'})
    return protocol, init


def test_read_only_connection_cannot_modify_store(corpus):
    path,_=corpus; before=hashlib.sha256(path.read_bytes()).hexdigest()
    with read_connection(path) as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute('DELETE FROM memory_blocks')
    assert hashlib.sha256(path.read_bytes()).hexdigest()==before


def test_missing_store_is_not_created(tmp_path):
    path=tmp_path/'missing.db'
    protocol,_=ready(RecallService(path,'repo-a'))
    result=protocol.handle({'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'recall_status'}})
    assert result['result']['isError'] and not path.exists()


def test_old_store_is_not_migrated(corpus):
    path,_=corpus
    c=sqlite3.connect(path); c.execute('PRAGMA user_version=8'); c.close()
    with pytest.raises(ValueError,match='schema differs'):
        RecallService(path,'repo-a').call('recall_status',{})
    c=sqlite3.connect(path); assert c.execute('PRAGMA user_version').fetchone()[0]==8; c.close()


def test_cross_agent_search_and_exact_pagination(corpus):
    path,ids=corpus; service=RecallService(path,'repo-a')
    hits=service.call('recall_search',{'query':'decision'})['hits']
    assert {h['agent'] for h in hits}=={'claude','codex'}
    first=service.call('recall_get',{'block_id':ids['accepted'],'max_chars':137})
    pieces=[first['text']]; start=first['next_start']
    while start is not None:
        page=service.call('recall_get',{'block_id':ids['accepted'],'start':start})
        pieces.append(page['text']); start=page['next_start']
    with read_connection(path) as c:
        assert ''.join(pieces)==memory.get_block(c,ids['accepted'],max_chars=100000)['text']


def test_scoped_status_counts_and_brief_do_not_leak_other_repositories(corpus):
    path,_=corpus; service=RecallService(path,'repo-a')
    result=service.call('recall_status',{})
    assert result['source_count']==2
    assert 'legacy_sessions' not in result
    with read_connection(path) as c:
        count=c.execute("SELECT count(*) FROM memory_chunks c JOIN memory_blocks b ON c.block_id=b.id JOIN memory_sources s ON s.source_key=b.source_key WHERE s.repo_id='repo-a'").fetchone()[0]
    assert result['semantic']['chunks']==count
    assert 'repo-b' not in json.dumps(service.call('recall_brief',{}))
    assert service.call('recall_search',{'query':'quasar'})['hits']==[]


def test_search_coverage_is_compact_without_hiding_missing_sources(corpus):
    path,_=corpus; service=RecallService(path,'repo-a')
    with read_connection(path) as c:
        source_path=c.execute("SELECT path FROM memory_sources WHERE source_key='claude:accepted'").fetchone()[0]
    Path(source_path).unlink()
    for tool, args in [('recall_search', {'query':'decision'}), ('recall_brief', {})]:
        coverage=service.call(tool,args)['coverage']
        assert coverage['source_count']==2 and coverage['checked_sources']==2
        assert coverage['checked_source_states']['source_missing']==1
        assert 'sources' not in coverage and len(json.dumps(coverage))<1500
        assert 'repo-b' not in json.dumps(coverage)
    # Full operational detail remains available on demand, with pagination.
    status=service.call('recall_status',{'limit':1})
    assert len(status['sources'])==1 and status['next_offset']==1


def test_compact_coverage_labels_partial_page_and_preserves_source_filter(corpus):
    path,_=corpus; service=RecallService(path,'repo-a')
    with read_connection(path) as c:
        coverage=service.coverage(c, limit=1, compact=True)
    assert coverage['source_count']==2 and coverage['checked_sources']==1 and coverage['next_offset']==1
    assert sum(coverage['checked_source_states'].values())==1
    scoped=service.call('recall_search',{'query':'decision','source':'codex:rejected'})['coverage']
    assert scoped['source_count']==scoped['checked_sources']==1 and scoped['next_offset'] is None


def test_get_and_source_filters_enforce_scope(corpus):
    path,ids=corpus; service=RecallService(path,'repo-a')
    with pytest.raises(ValueError,match='Unknown block in this repository'):
        service.call('recall_get',{'block_id':ids['hidden']})
    for tool in ('recall_search','recall_brief','recall_status'):
        args={'source':'codex:hidden'}
        if tool=='recall_search': args['query']='quasar'
        with pytest.raises(ValueError,match='Unknown source in this repository'):
            service.call(tool,args)
    assert service.call('recall_status',{'source':'codex:rejected'})['source_count']==1


@pytest.mark.parametrize('args',[{'query':'decision','limit':True},{'query':'x','limit':11},{'query':'x','db':'/other.db'}, {'query':'x','repo_id':'repo-b'}, {'query':'x','kind':'invalid'}, {'query':['x']}, {'query':'x','source':''}])
def test_untrusted_tool_arguments_cannot_widen_scope(corpus,args):
    with pytest.raises(ValueError): RecallService(corpus[0],'repo-a').call('recall_search',args)


def test_reader_observes_later_capture_commits(corpus):
    path,ids=corpus; service=RecallService(path,'repo-a')
    assert service.call('recall_search',{'query':'freshcanary'})['hits']==[]
    c=get_connection(path)
    source=c.execute("SELECT path FROM memory_sources WHERE source_key='codex:rejected'").fetchone()[0]
    with open(source,'a') as f: f.write(json.dumps({'type':'response_item','payload':{'id':'later','type':'message','role':'user','content':[{'type':'input_text','text':'freshcanary follow-up'}]}})+'\n')
    with open(source,'a') as f: f.write(json.dumps({'type':'response_item','payload':{'id':'command','type':'function_call','name':'Bash','arguments':'cargo test --release'}})+'\n')
    c.execute("UPDATE memory_sources SET scope_pinned=1 WHERE source_key='codex:rejected'")
    memory.index_file(c,source,agent='codex',session_id='rejected'); c.commit(); c.close()
    assert service.call('recall_search',{'query':'freshcanary'})['hits']
    commands=service.call('recall_search',{'query':'cargo test','kind':'tool_use'})['hits']
    assert commands and commands[0]['kind']=='tool_use'
    assert service.call('recall_get',{'block_id':commands[0]['block_id']})['text']=='Bash cargo test --release'


def test_protocol_negotiation_errors_and_notifications(corpus):
    protocol=Protocol(RecallService(corpus[0],'repo-a'))
    assert protocol.handle({'jsonrpc':'2.0','id':1,'method':'tools/list'})['error']['code']==-32000
    protocol,init=ready(protocol.service, 'future-version')
    assert init['result']['protocolVersion']=='2025-11-25'
    tools=protocol.handle({'jsonrpc':'2.0','id':2,'method':'tools/list'})['result']['tools']
    assert len(tools)==4 and all(t['annotations']['readOnlyHint'] for t in tools)
    assert protocol.handle({'jsonrpc':'2.0','method':'notifications/cancelled','params':{'requestId':99}}) is None
    assert protocol.handle({'jsonrpc':'2.0','id':3,'method':'not-a-method'})['error']['code']==-32601
    assert protocol.handle({'jsonrpc':'2.0','id':4,'method':'tools/call','params':{'name':'delete'}})['error']['code']==-32602


def test_older_protocol_uses_text_content(corpus):
    protocol,_=ready(RecallService(corpus[0],'repo-a'),'2024-11-05')
    response=protocol.handle({'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'recall_status'}})['result']
    assert not response['isError'] and 'structuredContent' not in response
    assert json.loads(response['content'][0]['text'])['source_count']==2


def test_bad_and_oversized_wire_messages_recover(corpus):
    protocol,_=ready(RecallService(corpus[0],'repo-a'))
    data=b'{bad json\n'+b'x'*(MAX_MESSAGE+1)+b'\n'+json.dumps({'jsonrpc':'2.0','id':5,'method':'ping'}).encode()+b'\n'
    out=io.StringIO(); serve(protocol,io.BytesIO(data),out)
    replies=[json.loads(line) for line in out.getvalue().splitlines()]
    assert [r.get('error',{}).get('code') for r in replies]==[-32700,-32600,None]
    assert replies[-1]['id']==5


def test_real_subprocess_stdio(corpus):
    path,_=corpus
    messages=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'test','version':'1'}}},
              {'jsonrpc':'2.0','method':'notifications/initialized'},
              {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'recall_search','arguments':{'query':'amber-sparrow'}}}]
    result=subprocess.run([sys.executable,'-S',str(Path(__file__).parents[1]/'scripts/recall_mcp.py'),'--db',str(path),'--repo-id','repo-a'],
                          input=''.join(json.dumps(m)+'\n' for m in messages),text=True,capture_output=True,timeout=10)
    assert result.returncode==0,result.stderr
    replies=[json.loads(line) for line in result.stdout.splitlines()]
    assert len(replies)==2 and replies[1]['result']['structuredContent']['hits']


def test_oversized_result_is_a_bounded_tool_error(corpus,monkeypatch):
    service=RecallService(corpus[0],'repo-a')
    monkeypatch.setattr(service,'call',lambda *_: {'large':'x'*70000})
    protocol,_=ready(service)
    result=protocol.handle({'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'recall_status'}})
    assert result['result']['isError'] and len(json.dumps(result))<500


def test_default_read_is_bounded_and_can_recover_the_remaining_passage(corpus):
    path,ids=corpus;service=RecallService(path,'repo-a')
    result=service.call('recall_get',{'block_id':ids['accepted']})
    assert len(result['text'])==2000 and not result['neighbors']
    next_page=service.call('recall_get',{'block_id':ids['accepted'],'start':result['next_start']})
    with read_connection(path) as c:
        full=memory.get_block(c,ids['accepted'],max_chars=4000)['text']
    assert result['text']+next_page['text']==full
