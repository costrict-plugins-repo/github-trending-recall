"""Private controller contract; real-host receipts are separate from these tests."""
import json
import io
import sys
import uuid
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
from db import get_connection
from recall_private_store import SessionStore
from recall_access import Lease
from recall_conversion import atomic_record
import recall_private_session as native
import recall_private_control as control


def test_claude_native_frames_are_bounded_before_read(monkeypatch):
    monkeypatch.setattr(native, 'MAX_FRAME', 64)
    class BoundedStream(io.StringIO):
        def readline(self, size=-1):
            assert size == 65
            return super().readline(size)
    stream = BoundedStream('x' * 1000 + '\n')
    with pytest.raises(ValueError, match='frame budget'):
        list(native.claude_events(stream))
    assert stream.tell() == 65


def test_claude_native_frames_skip_non_events():
    stream = io.StringIO('bad json\n[]\nnull\n{"type":"result"}\n')
    assert list(native.claude_events(stream)) == [{'type': 'result'}]


@pytest.fixture
def bound(tmp_path):
    sid=str(uuid.uuid4()); shared=get_connection(tmp_path/'shared.db')
    store=SessionStore.create(shared,tmp_path/'private','codex:'+sid,tmp_path); shared.close()
    profile=native.make_profile('codex',sid,tmp_path,store.shared_path,store.path)
    file=tmp_path/'profile.json';native.save_new(file,profile)
    host=object.__new__(native.CodexConnection)
    host.profile=profile;host.reader=store.reader()
    return store,profile,file,host


@pytest.mark.parametrize('identity', [None, 'foreign', ''])
def test_dynamic_host_identity_refused_before_retrieval(bound, identity):
    _,_,_,host=bound
    result=host.dynamic({'threadId':identity,'tool':'recall_status','arguments':{}})
    assert not result['success'] and 'no fork or subagent grant' in result['contentItems'][0]['text']


def test_dynamic_owner_cannot_override_identity_in_arguments(bound):
    store,profile,_,host=bound
    result=host.dynamic({'threadId':profile['owner'][6:],'tool':'recall_status','arguments':{}})
    assert result['success']
    result=host.dynamic({'threadId':profile['owner'][6:],'tool':'recall_status','arguments':{'owner':'foreign'}})
    assert not result['success']
    control.change(store,'revoke')
    assert not host.dynamic({'threadId':profile['owner'][6:],'tool':'recall_status','arguments':{}})['success']


@pytest.mark.parametrize('extra', [{'parentThreadId':'parent'}, {'forkedFromId':'parent'}, {'id':str(uuid.uuid4())}, {'cwd':'/tmp'}])
def test_resume_fork_subagent_or_scope_mismatch_refused(bound, extra):
    _,profile,_,_=bound
    thread={'id':profile['owner'][6:],'cwd':profile['cwd'],'path':None,**extra}
    with pytest.raises(ValueError): native.check_codex_thread(thread,profile)


def test_profile_replacement_and_concurrent_resume_refused(bound):
    _,profile,file,_=bound
    with pytest.raises(ValueError,match='exists'): native.save_new(file,profile)
    with Lease(file,create=True,exclusive=True):
        with pytest.raises(ValueError,match='active connections'): Lease(file,exclusive=True)
    assert native.load_profile(file)==profile
    file.chmod(0o644)
    with pytest.raises(ValueError,match='owner-only'):native.load_profile(file)


def test_claude_new_and_resume_keep_fixed_identity_and_disable_delegation(bound):
    _,profile,file,_=bound
    profile.update(agent='claude',owner='claude:'+profile['owner'][6:])
    cmd=native.claude_command('claude',file,profile,'mcp.json','settings.json')
    assert '--session-id' in cmd and '--resume' not in cmd
    assert cmd[cmd.index('--session-id')+1] == profile['owner'][7:]
    assert cmd[cmd.index('--disallowedTools')+1] == 'Agent,Task'
    assert 'Bash' in cmd[cmd.index('--tools')+1]
    assert not any('bypass' in arg or 'skip-permissions' in arg for arg in cmd)
    profile['started']=True
    cmd=native.claude_command('claude',file,profile,'mcp.json','settings.json')
    assert '--resume' in cmd and '--session-id' not in cmd and '--fork-session' not in cmd


def test_claude_hook_binds_exact_transcript_and_refuses_other_identity(tmp_path):
    sid=str(uuid.uuid4());c=get_connection(tmp_path/'shared.db')
    s=SessionStore.create(c,tmp_path/'private','claude:'+sid,tmp_path);c.close()
    profile=native.make_profile('claude',sid,tmp_path,s.shared_path,s.path)
    file=tmp_path/'profile.json';native.save_new(file,profile)
    transcript=tmp_path/(sid+'.jsonl')
    transcript.write_text(json.dumps({'type':'user','sessionId':sid,'uuid':'u','message':{'role':'user','content':'Native hook violet marker'}})+'\n')
    event={'session_id':sid,'transcript_path':str(transcript),'hook_event_name':'Stop'}
    native.claude_hook(file,event)
    assert s.reader().call('recall_search',{'query':'violet'})['hits']
    assert native.load_profile(file)['transcript']==str(transcript)
    with pytest.raises(ValueError,match='mismatch'):native.claude_hook(file,{**event,'session_id':str(uuid.uuid4())})
    other=tmp_path/'alternate.jsonl';other.write_text(transcript.read_text())
    with pytest.raises(ValueError,match='changed'):native.claude_hook(file,{**event,'transcript_path':str(other)})


def test_dynamic_budget_failure_returns_tool_error_without_crashing_controller(bound):
    import sqlite3
    _,profile,_,host=bound
    class Interrupted:
        def call(self,*args):raise sqlite3.OperationalError('interrupted')
    host.reader=Interrupted()
    result=host.dynamic({'threadId':profile['owner'][6:],'tool':'recall_search','arguments':{'query':'x'}})
    assert not result['success']
    assert '2-second budget' in result['contentItems'][0]['text']
