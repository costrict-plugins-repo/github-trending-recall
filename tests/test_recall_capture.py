"""Independent capture: real incremental adapters, fairness, retry and no implicit scan."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from db import get_connection
import memory_store as memory
from recall_capture import CaptureWorker


def record(key,text):
    return {'type':'response_item','payload':{'id':key,'type':'message','role':'user','content':[{'type':'input_text','text':text}]}}


@pytest.fixture
def worker(tmp_path):
    root=tmp_path/'authorized'; root.mkdir()
    c=get_connection(tmp_path/'store.db')
    yield CaptureWorker(c,[root],'codex'),root
    c.close()


def test_capture_new_file_and_append_without_any_claude_process(worker):
    w,root=worker; p=root/'rollout.jsonl'; p.write_text(json.dumps(record('one','accepted amber decision'))+'\n')
    result=w.refresh(2)
    assert result['blocks_updated']==1 and not result['errors']
    assert w.refresh(2)['passes']==0
    with p.open('a') as f: f.write(json.dumps(record('two','exact command cargo test'))+'\n')
    assert w.refresh(2)['blocks_updated']==1
    assert memory.search(w.conn,'cargo test')


def test_partial_record_retries_and_same_size_edit_requires_explicit_rebuild(worker):
    w,root=worker; p=root/'rollout.jsonl'; original=json.dumps(record('one','amber')).encode()
    p.write_bytes(original)
    assert next(iter(w.refresh(2)['states'].values()))['state']=='partial_record'
    p.write_bytes(original+b'\n'); assert w.refresh(2)['blocks_updated']==1
    p.write_bytes(original.replace(b'amber',b'viole')+b'\n')
    assert next(iter(w.refresh(2)['states'].values()))['state']=='source_changed'
    assert memory.search(w.conn,'amber') and not memory.search(w.conn,'viole')


def test_busy_source_cannot_starve_another_source(worker,monkeypatch):
    w,root=worker
    (root/'a.jsonl').write_text(''.join(json.dumps(record(str(i),'large file record '+str(i)))+'\n' for i in range(8)))
    (root/'b.jsonl').write_text(json.dumps(record('b','small file important decision'))+'\n')
    original=memory.index_file; order=[]
    def capped(conn,path,**kwargs):
        order.append(Path(path).name)
        return original(conn,path,max_bytes=1,**kwargs)
    monkeypatch.setattr(memory,'index_file',capped)
    result=w.refresh(2)
    assert order[:2]==['a.jsonl','b.jsonl'] and result['pending_files']==0
    assert memory.search(w.conn,'important')


def test_discovery_stays_in_authorized_roots(worker,tmp_path):
    w,root=worker; outside=tmp_path/'outside.jsonl'
    outside.write_text(json.dumps(record('outside','do not import outside corpus'))+'\n')
    (root/'linked.jsonl').symlink_to(outside)
    assert w.refresh(2)['passes']==0
    assert memory.search(w.conn,'outside')==[]


def test_missing_root_reports_failure(worker):
    w,root=worker; root.rmdir()
    assert w.refresh(2)['errors']==[{'path':str(root),'state':'source_missing'}]


def test_oneshot_cli_isolated_store(tmp_path):
    trace=tmp_path/'trace.jsonl'; trace.write_text(json.dumps(record('cli','portable capture test'))+'\n')
    p=subprocess.run([sys.executable,str(Path(__file__).parents[1]/'scripts/recall_capture.py'),'--db',str(tmp_path/'cli.db'),
                      '--agent','codex','--path',str(trace)],text=True,capture_output=True,timeout=10)
    assert p.returncode==0,p.stderr
    assert json.loads(p.stdout)['blocks_updated']==1
