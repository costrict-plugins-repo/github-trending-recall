"""Observed quotation/provenance, legacy host content and Cowork scope regressions."""
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path[:0] = [str(Path(__file__).parents[1]/p) for p in ('scripts','hooks')]
import memory_store as m
from db import get_connection, insert_session, insert_exchanges, insert_tag, search_exchanges_fts
from recall_mcp import RecallService
from recall_capture import CaptureWorker
from prompt_submit import parse_transcript_from_offset, index_transcript
from legacy_host_cleanup import clean


def record(text, key='one', role='user', **kw):
    return dict(type=role, uuid=key, sessionId='sample', timestamp='2026-09-06T01:00:00Z',
                message={'role':role,'content':text}, **kw)


def write(path, *rows):
    with path.open('a') as f:
        for row in rows: f.write(json.dumps(row)+'\n')


@pytest.fixture
def store(tmp_path):
    c=get_connection(tmp_path/'store.db')
    yield c,tmp_path
    c.close()


def test_quote_verification_uses_exact_unicode_offsets_and_window(store):
    c,p=store; trace=p/'trace.jsonl'; text='🙂 café\x00 old words; exact quote; tail'
    write(trace,record(text)); m.index_file(c,trace,cwd=str(p)); c.commit()
    bid=m.search(c,'exact')[0]['block_id']
    r=m.get_block(c,bid,quote='exact quote')
    check=r['citation_check']; assert check['valid']
    assert text[check['quote_start']:check['quote_end']]=='exact quote'
    assert not m.get_block(c,bid,start=0,max_chars=6,quote='exact quote')['citation_check']['valid']
    assert not m.get_block(c,bid,quote='invented quote')['citation_check']['valid']


def test_changed_revision_rejects_prior_hash_even_if_quote_survives(store):
    c,p=store; trace=p/'trace.jsonl'; write(trace,record('accepted amber'))
    m.index_file(c,trace,cwd=str(p)); c.commit()
    hit=m.search(c,'amber')[0]
    trace.write_text(json.dumps(record('accepted amber with a later qualification'))+'\n')
    m.index_file(c,trace,cwd=str(p),rebuild=True); c.commit()
    r=m.get_block(c,hit['block_id'],quote='accepted amber',expected_hash=hit['content_hash'])
    assert r['citation_check']['quote_matches'] and not r['citation_check']['valid']
    assert not r['citation_check']['version_matches']


def test_mcp_quote_check_preserves_scope_and_read_only_behavior(store):
    c,p=store; trace=p/'trace.jsonl'; write(trace,record('accepted amber'))
    m.index_file(c,trace,cwd=str(p)); c.commit(); bid=m.search(c,'amber')[0]['block_id']
    service=RecallService(p/'store.db',m.repository_identity(str(p)))
    before=c.total_changes
    result=service.call('recall_get',{'block_id':bid,'quote':'accepted amber','max_chars':20})
    assert result['citation_check']['valid'] and c.total_changes==before
    with pytest.raises(ValueError,match='Unknown block in this repository'):
        RecallService(p/'store.db','foreign-repo').call('recall_get',{'block_id':bid,'quote':'accepted amber'})


def test_tool_input_is_not_execution_or_file_state_evidence(store):
    c,p=store; trace=p/'trace.jsonl'
    write(trace,record([{'type':'tool_use','id':'tool1','name':'Bash','input':{'command':'assert patch_succeeded'}}],role='assistant'))
    m.index_file(c,trace,cwd=str(p)); c.commit()
    hit=m.search(c,'patch_succeeded',kind='tool_use')[0]
    r=m.get_block(c,hit['block_id'],quote='assert patch_succeeded')
    assert r['citation_check']['valid']
    assert r['provenance'].startswith('tool_request:') and 'not successful execution' in r['provenance']
    assert hit['provenance']==r['provenance']


def test_legacy_host_only_pass_does_not_create_a_turn_or_lose_continuation(store):
    c,p=store; trace=p/'legacy.jsonl'
    write(trace,record('real ask'),record('first answer',role='assistant'))
    index_transcript(c,'sample',str(trace),str(p),'hash'); c.commit()
    write(trace,record('HOST_SKILL_BODY',isMeta=True),record('HOST_SUMMARY',isCompactSummary=True))
    messages,end=parse_transcript_from_offset(str(trace),c.execute('SELECT byte_offset FROM sessions').fetchone()[0])
    assert messages==[] and end==trace.stat().st_size
    index_transcript(c,'sample',str(trace),str(p),'hash'); c.commit()
    write(trace,record('continued answer',role='assistant'))
    index_transcript(c,'sample',str(trace),str(p),'hash'); c.commit()
    rows=c.execute('SELECT * FROM exchanges').fetchall()
    assert len(rows)==1 and rows[0]['user_text']=='real ask'
    assert 'first answer' in rows[0]['assistant_text'] and 'continued answer' in rows[0]['assistant_text']
    assert not search_exchanges_fts(c,'HOST_SKILL_BODY')
    assert not search_exchanges_fts(c,'HOST_SUMMARY')


def test_host_cleanup_preserves_indices_replies_and_manual_annotations(store):
    c,p=store; trace=p/'legacy.jsonl'; write(trace,record('UNIQUE_HOST',isMeta=True))
    insert_session(c,'sample',str(p),'hash','now',transcript_path=str(trace))
    insert_exchanges(c,'sample',[{'idx':7,'timestamp':'2026-09-06T01:00:00Z','preview':'UNIQUE_HOST','user_text':'UNIQUE_HOST','assistant_text':'real reply','tool_text':'Bash original'}])
    before=dict(c.execute('SELECT * FROM exchanges').fetchone())
    insert_tag(c,'manual-keep','sample',exchange_idx=7)
    tags=[dict(r) for r in c.execute('SELECT * FROM tags')]
    assert clean(c,'sample')['matched']==1
    assert dict(c.execute('SELECT * FROM exchanges').fetchone())==before
    assert clean(c,'sample',apply=True)['matched']==1; c.commit()
    after=dict(c.execute('SELECT * FROM exchanges').fetchone())
    assert {k:v for k,v in before.items() if k not in ('user_text','preview')}=={k:v for k,v in after.items() if k not in ('user_text','preview')}
    assert not search_exchanges_fts(c,'UNIQUE_HOST') and search_exchanges_fts(c,'reply')
    assert clean(c,'sample',apply=True)['matched']==0
    assert [dict(r) for r in c.execute('SELECT * FROM tags')]==tags
    c.execute("INSERT INTO exchanges_fts(exchanges_fts,rank) VALUES('integrity-check',1)")


def test_ambiguous_host_cleanup_never_removes_matching_real_user_record(store):
    c,p=store; trace=p/'legacy.jsonl'; write(trace,record('same',isMeta=True),record('same'))
    insert_session(c,'sample',str(p),'hash','now',transcript_path=str(trace))
    insert_exchanges(c,'sample',[{'idx':1,'timestamp':'2026-09-06T01:00:00Z','preview':'same','user_text':'same','assistant_text':'answer'}])
    r=clean(c,'sample',apply=True)
    assert r['matched']==0 and r['ambiguous_keys_skipped']==1


def test_cowork_explicit_host_mapping_survives_append_and_refuses_foreign_rescope(store):
    c,p=store; trace=p/'cowork.jsonl'; write(trace,record('first decision',cwd='/sessions/vm-project'))
    host=p/'host-project'; host.mkdir()
    w=CaptureWorker(c,[trace],'claude',cwd=host)
    assert w.refresh(2)['blocks_updated']==1
    row=c.execute('SELECT * FROM memory_sources').fetchone()
    assert row['repo_id']==m.repository_identity(str(host)) and row['scope_pinned']==1
    write(trace,record('second decision','two',cwd='/sessions/changed-vm-path'))
    assert CaptureWorker(c,[trace],'claude').refresh(2)['blocks_updated']==1
    assert c.execute('SELECT repo_id FROM memory_sources').fetchone()[0]==row['repo_id']
    foreign=p/'foreign-project'; foreign.mkdir()
    r=CaptureWorker(c,[trace],'claude',cwd=foreign).refresh(2)
    assert r['errors'][0]['state']=='scope_mismatch'
    assert c.execute('SELECT repo_id FROM memory_sources').fetchone()[0]==row['repo_id']


def test_cleanup_refuses_busy_writer_before_reading_fts_payload(store):
    c,p=store; trace=p/'legacy.jsonl'; write(trace,record('HOST_BUSY',isMeta=True))
    insert_session(c,'sample',str(p),'hash','now',transcript_path=str(trace))
    insert_exchanges(c,'sample',[{'idx':1,'timestamp':'2026-09-06T01:00:00Z','preview':'HOST_BUSY','user_text':'HOST_BUSY','assistant_text':'before'}]); c.commit()
    other=get_connection(p/'store.db'); other.execute('PRAGMA busy_timeout=10')
    try:
        c.execute('BEGIN IMMEDIATE')
        with pytest.raises(sqlite3.OperationalError,match='locked'):
            clean(other,'sample',apply=True)
        c.rollback()
        c.execute("UPDATE exchanges SET assistant_text='arriving continuation' WHERE session_id='sample'")
        c.execute("INSERT INTO exchanges_fts(exchanges_fts) VALUES('rebuild')"); c.commit()
        assert clean(other,'sample',apply=True)['matched']==1; other.commit()
        assert other.execute('SELECT assistant_text FROM exchanges').fetchone()[0]=='arriving continuation'
        other.execute("INSERT INTO exchanges_fts(exchanges_fts,rank) VALUES('integrity-check',1)")
    finally:
        c.rollback(); other.close()
