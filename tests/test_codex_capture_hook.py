"""Exact-source lifecycle capture, privacy refusal and config preservation."""
import json,sys,shlex,subprocess
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
from db import get_connection
from recall_codex_hook import capture
from prepare_codex_hooks import prepare
import recall_privacy as policy
import memory_store as m


def fixture(tmp_path):
    p=tmp_path/'root'/'s.jsonl';p.parent.mkdir()
    p.write_text(json.dumps({'type':'session_meta','payload':{'id':'s','cwd':str(tmp_path)}})+'\n')
    event={'hook_event_name':'SessionStart','session_id':'s','transcript_path':str(p),'cwd':str(tmp_path)}
    return p,event,tmp_path/'store.db'


def append(p,key,text):
    with p.open('a') as f:f.write(json.dumps({'type':'response_item','payload':{'type':'message','id':key,'role':'user','content':[{'type':'input_text','text':text}]}})+'\n')


def test_root_start_and_stop_capture_without_claude(tmp_path):
    p,event,db=fixture(tmp_path);append(p,'u','first decision amber')
    assert capture(event,db,p.parent)['result']['state']=='complete'
    append(p,'a','second decision violet');event['hook_event_name']='Stop'
    assert capture(event,db,p.parent)['result']['state']=='complete'
    c=get_connection(db);assert m.search(c,'violet') and m.search(c,'amber');c.close()


@pytest.mark.parametrize('case',['mismatched-id','subagent-event','foreign-path','symlink','missing-metadata'])
def test_bad_routing_refused_before_store_creation(tmp_path,case):
    p,event,db=fixture(tmp_path)
    if case=='mismatched-id':event['session_id']='foreign'
    if case=='subagent-event':event['hook_event_name']='SubagentStop'
    if case=='foreign-path':
        other=tmp_path/'other.jsonl';other.write_bytes(p.read_bytes());event['transcript_path']=str(other)
    if case=='symlink':
        link=p.parent/'link.jsonl';link.symlink_to(p);event['transcript_path']=str(link)
    if case=='missing-metadata':p.write_text('{}\n')
    with pytest.raises(ValueError):capture(event,db,p.parent)
    assert not db.exists()


def test_capture_off_remains_off_at_stop(tmp_path):
    p,event,db=fixture(tmp_path);append(p,'u','private secret')
    c=get_connection(db);policy.set_mode(c,'codex:s','off');c.close()
    assert capture(event,db,p.parent)['result']['state']=='capture_disabled'
    c=get_connection(db);assert not m.search(c,'private');c.close()


def test_hook_does_not_guess_parent_directory_mapping(tmp_path):
    p,event,db=fixture(tmp_path);target=tmp_path/'target';target.mkdir()
    with pytest.raises(ValueError,match='Ambiguous'):capture(event,db,p.parent,cwd=target)
    c=get_connection(db);assert not c.execute('SELECT * FROM memory_sources').fetchall();c.close()


def test_preparer_preserves_hooks_quotes_paths_and_does_not_install(tmp_path):
    root=tmp_path/'source root';root.mkdir();existing=tmp_path/'old.json'
    old={'hooks':{'Stop':[{'hooks':[{'type':'command','command':'existing'}]}]},'description':'Keep me'}
    existing.write_text(json.dumps(old));output=tmp_path/'prepared.json';db=tmp_path/'uncreated.db'
    r=prepare(output,db,root,existing=existing)
    assert not r['installed'] and not r['trusted'] and not db.exists()
    data=json.loads(output.read_text());assert data['hooks']['Stop'][0]==old['hooks']['Stop'][0]
    cmd=shlex.split(data['hooks']['Stop'][1]['hooks'][0]['command']);assert cmd[cmd.index('--root')+1]==str(root)
    assert json.loads(existing.read_text())==old
    with pytest.raises(ValueError,match='overwrite'):prepare(output,db,root)
