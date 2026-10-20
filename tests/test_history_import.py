"""Offline import contracts, including branches, scope and atomic replacement."""
import json
import sqlite3
import sys
import zipfile
from pathlib import Path

import pytest
sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
import memory_history as h
import memory_store as m
from db import get_connection


def claude():
    return {'uuid':'c1','name':'Example','chat_messages':[
        {'uuid':'u1','sender':'human','created_at':'2026-09-06','text':'Please use amber 🙂'},
        {'uuid':'a1','sender':'assistant','content':[{'type':'thinking','thinking':'hiddenthought'}, {'type':'text','text':'amber confirmed'},{'type':'tool_use','input':'toolsecret'}]}]}


def save(p,data):p.write_text(json.dumps(data));return p


def test_preview_does_not_print_text_and_explicit_import_redacts(tmp_path):
    obj=claude();obj['chat_messages'][0]['text']='my password is hunter-two'
    p=save(tmp_path/'conversations.json',[obj]);preview=h.preview(h.load(p))
    assert 'hunter-two' not in json.dumps(preview)
    c=get_connection(tmp_path/'store.db');r=h.ingest(c,p,'claude-export','c1',str(tmp_path));c.commit()
    assert r['coverage']['excluded_items']==2
    assert not m.search(c,'hiddenthought') and not m.search(c,'toolsecret')
    assert 'hunter-two' not in c.execute('SELECT group_concat(text) FROM memory_blocks').fetchone()[0]
    status=m.status(c)['sources'][0]
    assert status['state']=='imported_snapshot' and 'freshness is unknown' in status['next_action']
    p.unlink();assert m.status(c)['sources'][0]['state']=='imported_snapshot'
    c.close()


def test_snapshot_refresh_is_atomic_keeps_history_and_refuses_other_scope(tmp_path):
    p=save(tmp_path/'export.json',[claude()]);c=get_connection(tmp_path/'store.db')
    h.ingest(c,p,'claude-export','c1',str(tmp_path));c.commit();old=m.search(c,'confirmed')[0]
    obj=claude();obj['chat_messages']=obj['chat_messages'][:1];obj['chat_messages'][0]['text']='changed violet'
    save(p,[obj]);h.ingest(c,p,'claude-export','c1',str(tmp_path));c.commit()
    assert not m.search(c,'confirmed') and m.search(c,'violet')
    assert m.get_block(c,old['block_id'],revision=old['content_hash'])['text']=='amber confirmed'
    foreign=tmp_path/'foreign';foreign.mkdir()
    with pytest.raises(ValueError,match='different scope'):h.ingest(c,p,'claude-export','c1',str(foreign))
    assert m.search(c,'violet');c.close()


def test_bad_late_message_and_duplicate_conversation_write_nothing(tmp_path):
    obj=claude();obj['chat_messages'].append({'uuid':'bad','sender':'human','content':42})
    p=save(tmp_path/'export.json',[obj]);c=get_connection(tmp_path/'store.db')
    with pytest.raises(ValueError):h.ingest(c,p,'claude-export','c1',str(tmp_path))
    assert not c.execute('SELECT * FROM memory_sources').fetchall()
    save(p,[claude(),claude()])
    with pytest.raises(ValueError,match='Duplicate conversation'):h.load(p)
    c.close()


def gpt():
    def node(mid,text,parent,role='assistant',**kwargs):
        return {'parent':parent,'message':{'id':mid,'author':{'role':role},'content':{'content_type':'text','parts':[text]},**kwargs}}
    return {'id':'g1','current_node':'new','mapping':{
        'root':node('r','systemsecret',None,'system'),
        'user':node('u','select delivery label','root','user'),
        'old':node('o','abandoned violet','user'),
        'new':node('n','accepted amber','user')}}


def test_chatgpt_selects_current_branch_and_rejects_cycles(tmp_path):
    obj=gpt();turns,coverage=h.messages(obj)
    assert [r[-1] for r in turns]==['select delivery label','accepted amber']
    assert coverage=={'excluded_items':1,'inactive_branch_nodes':1,'retained_text_blocks':2}
    obj['mapping']['user']['parent']='new'
    with pytest.raises(ValueError,match='cyclic'):h.messages(obj)
    obj=gpt();del obj['current_node']
    with pytest.raises(ValueError,match='no branch guessed'):h.messages(obj)


def test_zip_splits_no_extraction_and_uncompressed_budget(tmp_path):
    p=tmp_path/'export.zip'
    with zipfile.ZipFile(p,'w',compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr('data/conversations-000.json',json.dumps([gpt()]))
        z.writestr('../../conversations-001.json',json.dumps([claude()]))
        z.writestr('../../unrelated.txt','private content')
    assert len(h.load(p))==2 and not (tmp_path/'unrelated.txt').exists()
    with pytest.raises(ValueError,match='byte budget'):h.load(p,30)


def test_rendered_snapshot_provenance_and_unicode_offsets(tmp_path):
    obj={'format':'recall-chat-snapshot-v1','id':'chat-123','surface':'claude-chat',
         'provenance':'User copied visible prose; tool card omitted','messages':[
             {'id':'turn1','role':'user','text':'A🙂\x00B'}]}
    p=save(tmp_path/'chat.json',obj);c=get_connection(tmp_path/'store.db')
    h.ingest(c,p,'app-snapshot','chat-123',str(tmp_path));c.commit()
    bid=c.execute('SELECT id FROM memory_blocks').fetchone()[0]
    result=m.get_block(c,bid,quote='🙂\x00')
    assert result['citation_check']['quote_start']==1 and result['citation_check']['quote_end']==3
    del obj['provenance'];save(p,obj)
    with pytest.raises(ValueError,match='provenance'):h.load(p)
    c.close()


def test_unknown_input_refused_and_preview_cli_never_opens_db(tmp_path):
    import subprocess
    p=save(tmp_path/'export.json',[claude()]);db=tmp_path/'absent.db'
    r=subprocess.run([sys.executable,str(Path(__file__).parents[1]/'scripts/recall_memory.py'),'--db',str(db),'history-preview',str(p)],capture_output=True,text=True)
    assert r.returncode==0 and not db.exists() and json.loads(r.stdout)['conversations'][0]['id']=='c1'
    save(p,{'unknown':[]})
    with pytest.raises(ValueError):h.load(p)
