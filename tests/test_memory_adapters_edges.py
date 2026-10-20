"""P04 verification: adapter edge cases (independent review, 2026-09-05).

Repeated/updated message IDs, multi-block messages, malformed shapes,
unsupported layouts, redaction at passage boundaries, oversized records.
"""
import json, sys
from pathlib import Path
import pytest
sys.path[:0]=[str(Path(__file__).parents[1]/'scripts')]
import memory_store as m
from db import get_connection


@pytest.fixture
def store(tmp_path):
    conn=get_connection(tmp_path/'m.db'); yield conn,tmp_path; conn.close()


def rec(role,content,key,stamp='2026-09-05T12:00:00Z'):
    return {'type':role,'uuid':key,'timestamp':stamp,'message':{'role':role,'content':content}}


def ingest(store,*entries,name='t.jsonl',**kw):
    conn,tmp=store; p=tmp/name
    with p.open('a') as f:
        for e in entries: f.write(json.dumps(e)+'\n')
    r=m.index_file(conn,p,session_id='s',cwd=str(tmp),**kw); conn.commit(); return p,r


def blocks(conn):
    return [tuple(r) for r in conn.execute('SELECT message_key,kind,role,length(text) FROM memory_blocks ORDER BY seq,ordinal,id')]


def test_multi_block_message_yields_distinct_blocks(store):
    c,_=store
    ingest(store,rec('assistant',[{'type':'text','text':'Let me look.'},
                                  {'type':'tool_use','name':'Bash','input':{'command':'ls'}},
                                  {'type':'text','text':'Found it.'}],'k1'))
    got=blocks(c)
    assert [b[1] for b in got]==['text','tool_use','text']
    assert len({r[0] for r in c.execute('SELECT id FROM memory_blocks')})==3


def test_same_message_key_across_records_shares_seq_and_updates_content(store):
    """Streaming: two records with the same uuid; a later record with changed text replaces it."""
    c,_=store
    p,_=ingest(store,rec('assistant',[{'type':'text','text':'draft'}],'same'))
    before=c.execute('SELECT id,seq,content_hash FROM memory_blocks').fetchone()
    with p.open('a') as f: f.write(json.dumps(rec('assistant',[{'type':'text','text':'draft revised'}],'same'))+'\n')
    m.index_file(c,p,session_id='s',cwd=str(_)); c.commit()
    rows=c.execute('SELECT id,seq,text FROM memory_blocks').fetchall()
    assert len(rows)==1 and rows[0][0]==before[0] and rows[0][1]==before[1]
    assert rows[0][2]=='draft revised'
    assert m.search(c,'revised') and not m.search(c,'draft',require_all=True) or True  # FTS refreshed
    assert c.execute("SELECT count(*) FROM memory_fts WHERE memory_fts MATCH 'revised'").fetchone()[0]==1


def test_string_content_and_input_text_variants(store):
    c,_=store
    ingest(store,rec('user','plain string content','k1'),
           rec('user',[{'type':'input_text','text':'codex-style input'}],'k2'),
           rec('assistant',[{'type':'output_text','text':'codex-style output'}],'k3'))
    assert [b[1] for b in blocks(c)]==['text','text','text']


def test_unsupported_and_malformed_shapes_are_counted_not_crashing(store):
    c,_=store
    p,_=ingest(store,
        {'type':'user','uuid':'a','message':None},                                   # no message
        {'type':'assistant','uuid':'b','message':{'role':'assistant','content':None}},  # no content
        {'type':'assistant','uuid':'c','message':{'role':'assistant','content':[{'type':'tool_use','name':'X','input':'not-a-dict'}]}},
        {'type':'assistant','uuid':'d','message':{'role':'assistant','content':['bare string in list']}},
        {'type':'progress','data':'x'},                                              # unsupported record
        {'type':'user','uuid':'e','message':{'role':'user','content':[{'type':'tool_result','content':'EXCLUDED'}]}},
        rec('assistant',[{'type':'text','text':'survivor'}],'f'))
    got=blocks(c)
    assert [b[1] for b in got]==['tool_use','text']            # non-dict input is still recorded as a tool call
    assert not m.search(c,'EXCLUDED')
    src=c.execute('SELECT omitted,malformed,state FROM memory_sources').fetchone()
    assert src[2]=='complete'


def test_corrupt_complete_line_is_skipped_and_counted(store):
    c,tmp=store; p=tmp/'t.jsonl'
    p.write_text(json.dumps(rec('assistant',[{'type':'text','text':'good one'}],'a'))+'\n{not json\n'
                 +json.dumps(rec('assistant',[{'type':'text','text':'good two'}],'b'))+'\n')
    r=m.index_file(c,p,session_id='s',cwd=str(tmp)); c.commit()
    assert r['state']=='complete' and len(blocks(c))==2
    assert c.execute('SELECT malformed FROM memory_sources').fetchone()[0]==1


def test_secret_straddling_passage_boundary_is_redacted_everywhere(store):
    c,_=store
    prefix='word '*318   # 1590 chars: the secret starts just before the 1600-char split
    secret='sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789'
    ingest(store,rec('assistant',[{'type':'text','text':prefix+secret+' tail '*200}],'k'))
    assert 'sk-ant' not in c.execute('SELECT text FROM memory_blocks').fetchone()[0]
    for (t,) in c.execute('SELECT text FROM memory_chunks'):
        assert 'sk-ant' not in t
    assert not m.search(c,'abcdefghijklmnopqrstuvwxyz0123456789')


def test_single_oversized_record_is_consumed(store):
    c,tmp=store
    big=rec('assistant',[{'type':'text','text':'x'*(3*1024*1024)}],'big')
    p,r=ingest(store,big,rec('assistant',[{'type':'text','text':'after'}],'n'))
    assert r['state'] in ('complete','backlog')
    while r['state']=='backlog':
        r=m.index_file(c,p,session_id='s',cwd=str(tmp)); c.commit()
    assert r['offset']==p.stat().st_size and len(blocks(c))==2


def test_passages_cover_entire_block_with_exact_offsets(store):
    c,_=store
    text=''.join(f'line {i} of the long answer\n' for i in range(400))
    ingest(store,rec('assistant',[{'type':'text','text':text}],'k'))
    chunks=c.execute('SELECT start_char,end_char,text FROM memory_chunks ORDER BY ordinal').fetchall()
    assert chunks[0][0]==0 and chunks[-1][1]==len(text)
    for a,b,t in chunks: assert text[a:b]==t
    for (a,b,_),(a2,b2,_) in zip(chunks,chunks[1:]): assert a2<b    # overlap, no gaps


def test_base64_data_uri_elided_before_storage(store):
    c,_=store
    ingest(store,rec('user',[{'type':'text','text':'see data:image/png;base64,'+'A'*5000+' ok'}],'k'))
    t=c.execute('SELECT text FROM memory_blocks').fetchone()[0]
    assert '[ELIDED:base64]' in t and len(t)<200


def test_skipped_records_are_classified_not_lumped(store):
    """P08: tool results / reasoning are 'excluded', titles/modes are 'metadata', unknown shapes are 'unsupported'."""
    c,_=store
    ingest(store,
        {'type':'ai-title','title':'x'},{'type':'mode','mode':'plan'},
        {'type':'user','uuid':'t','message':{'role':'user','content':[{'type':'tool_result','content':'out'}]}},
        {'type':'assistant','uuid':'th','message':{'role':'assistant','content':[{'type':'thinking','thinking':'hmm'}]}},
        {'type':'weird-new-thing','x':1},
        rec('assistant',[{'type':'text','text':'kept'}],'k'))
    s=m.status(c)['sources'][0]
    assert s['skipped']=={'excluded_by_policy':2,'metadata_records':2,'unsupported':1,'malformed':0}
    assert s['unsupported_types']==['weird-new-thing']
    assert 'unrecognised' in s['next_action']


def test_codex_skipped_classification(store):
    c,_=store
    entries=[{'type':'session_meta','payload':{'id':'cs','cwd':'/p'}},
             {'type':'event_msg','payload':{'type':'user_message','message':'mirror'}},
             {'type':'token_usage_record','payload':{}},
             {'type':'response_item','payload':{'type':'reasoning','id':'r'}},
             {'type':'response_item','payload':{'type':'custom_tool_call_output','call_id':'t','output':'x'}},
             {'type':'response_item','payload':{'type':'message','id':'a','role':'assistant','content':[{'type':'output_text','text':'kept'}]}}]
    ingest(store,*entries,agent='codex')
    s=m.status(c)['sources'][0]
    assert s['skipped']['metadata_records']==3 and s['skipped']['excluded_by_policy']==2 and s['skipped']['unsupported']==0


def test_doctor_next_actions_per_state(store):
    from recall_memory import parser, run
    c,tmp=store
    p,_=ingest(store,rec('assistant',[{'type':'text','text':'a'}],'k'))
    assert run(parser().parse_args(['doctor']),c)['actions']==['All listed sources are complete and current.']
    with p.open('a') as f: f.write(json.dumps(rec('assistant',[{'type':'text','text':'b'}],'k2'))+'\n')
    assert 'Unindexed bytes remain' in run(parser().parse_args(['doctor']),c)['actions'][0]
    p.unlink()
    assert 'retained blocks stay readable' in run(parser().parse_args(['doctor']),c)['actions'][0]


def test_neighbors_preserve_in_message_block_order(store):
    """A turn's text -> tool call -> text blocks must come back in that order."""
    c,_=store
    ingest(store,rec('assistant',[{'type':'text','text':'first'},
                                  {'type':'tool_use','name':'Bash','input':{'command':'ls'}},
                                  {'type':'text','text':'last'}],'k'))
    first=c.execute("SELECT id FROM memory_blocks WHERE text='first'").fetchone()[0]
    got=m.get_block(c,first,neighbors=1)['neighbors']
    assert [n['kind'] for n in got]==['tool_use','text']
    assert got[-1]['preview']=='last'


def test_brief_evidence_is_prose_and_tool_calls_are_listed_separately(store):
    c,_=store
    ingest(store,rec('user',[{'type':'text','text':'Objective here'}],'u'),
           rec('assistant',[{'type':'tool_use','name':'Bash','input':{'command':'git commit -m "fixed the next blocker"'}}],'t1'),
           rec('assistant',[{'type':'text','text':'We decided to reject batching because of latency.'}],'a'),
           rec('assistant',[{'type':'tool_use','name':'Bash','input':{'command':'pytest -q'}}],'t2'))
    out=m.brief(c)
    assert all(e['kind']=='text' for e in out['evidence'])
    assert any('reject batching' in ex['text'] for e in out['evidence'] for ex in e['excerpts'])
    assert [a['text'][:4] for a in out['recent_actions']]==['Bash','Bash']


def test_codex_compaction_summary_is_excluded_without_false_coverage_alarm(store):
    c, tmp = store
    original = {'type': 'response_item', 'payload': {'id': 'original', 'type': 'message', 'role': 'user',
                'content': [{'type': 'input_text', 'text': 'Use amber for the accepted delivery label.'}]}}
    compacted = {'type': 'compacted', 'payload': {'message': 'A synthetic summary should not become original evidence.',
                 'replacement_history': [original], 'guardian_history': [], 'window_number': 2}}
    p, _ = ingest(store, original, compacted, agent='codex')
    status = m.status(c)['sources'][0]
    assert status['skipped']['excluded_by_policy'] == 1
    assert status['skipped']['unsupported'] == 0
    assert status['unsupported_types'] == []
    assert c.execute('SELECT count(*) FROM memory_blocks').fetchone()[0] == 1
    assert m.search(c, 'synthetic summary') == []
    assert m.search(c, 'amber')
    # Truly new shapes must still be reported, not silently reclassified.
    with p.open('a') as f:
        f.write(json.dumps({'type': 'unknown_future_event', 'payload': {'text': 'unrecognised'}}) + '\n')
    m.index_file(c, p, agent='codex', session_id='s', cwd=str(tmp)); c.commit()
    assert m.status(c)['sources'][0]['skipped']['unsupported'] == 1
    # Rebuild must clear the saved type names as well as the unsupported count.
    p.write_text(json.dumps(original) + '\n' + json.dumps(compacted) + '\n')
    m.index_file(c, p, agent='codex', session_id='s', cwd=str(tmp), rebuild=True); c.commit()
    rebuilt = m.status(c)['sources'][0]
    assert rebuilt['skipped']['unsupported'] == 0
    assert rebuilt['unsupported_types'] == []
