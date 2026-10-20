"""P52: complete published snapshots and recoverable revision-specific citations."""
import json
import sqlite3
from pathlib import Path
import sys

import pytest
sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
from db import get_connection,get_read_connection,SCHEMA_VERSION
import memory_store as m
import memory_revisions as rev
from memory_transfer import export_source,import_source
from recall_mcp import RecallService


def row(key,text):
    return {'type':'user','uuid':key,'sessionId':'rev','timestamp':'2026-09-06T00:00:00Z','message':{'role':'user','content':text}}


def write(p,*rows):p.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows))


@pytest.fixture
def store(tmp_path):
    c=get_connection(tmp_path/'store.db');p=tmp_path/'trace.jsonl'
    write(p,row('a','original amber decision 🙂\x00 exact tail'),row('b','old second evidence'))
    m.index_file(c,p,cwd=str(tmp_path));c.commit()
    yield c,p,tmp_path
    c.close()


def test_rebuild_keeps_complete_search_get_brief_and_read_snapshot(store):
    c,p,tmp=store;old=m.search(c,'amber')[0]; reader=get_read_connection(tmp/'store.db')
    write(p,row('a','replacement violet decision'),row('c','brandnew final evidence'))
    first=m.index_file(c,p,cwd=str(tmp),rebuild=True,max_records=1);c.commit()
    assert first['state']=='rebuilding'
    assert m.get_block(c,old['block_id'])['text'].startswith('original')
    assert m.search(c,'amber') and not m.search(c,'violet')
    assert not any('replacement' in x['text'] for e in m.brief(c)['evidence'] for x in e['excerpts'])
    assert m.index_file(c,p,cwd=str(tmp))['state']=='complete';c.commit()
    assert not m.search(c,'amber') and m.search(c,'violet')
    assert m.get_block(reader,old['block_id'])['text'].startswith('original')
    reader.close()
    got=m.get_block(c,old['block_id'],revision=old['content_hash'],quote='🙂\x00 exact tail')
    assert got['citation_check']['valid'] and got['text'][got['citation_check']['quote_start']:got['citation_check']['quote_end']]=='🙂\x00 exact tail'


def test_deleted_revision_remains_scoped_readable_but_not_in_current_search(store):
    c,p,tmp=store;old=m.search(c,'second')[0]
    write(p,row('a','replacement only'));m.index_file(c,p,cwd=str(tmp),rebuild=True);c.commit()
    with pytest.raises(ValueError,match='Unknown block'):m.get_block(c,old['block_id'])
    assert m.get_block(c,old['block_id'],revision=old['content_hash'])['text']=='old second evidence'
    service=RecallService(tmp/'store.db',m.repository_identity(str(tmp)))
    assert service.call('recall_get',{'block_id':old['block_id'],'revision':old['content_hash'],'quote':'second'})['citation_check']['valid']
    with pytest.raises(ValueError,match='Unknown block in this repository'):
        RecallService(tmp/'store.db','foreign').call('recall_get',{'block_id':old['block_id'],'revision':old['content_hash']})


def test_retention_pin_unpin_and_prune_cover_archives_and_staging(store):
    c,p,tmp=store;old=m.search(c,'amber')[0];rev.pin(c,old['block_id'],old['content_hash']);c.commit()
    for i in range(7):
        write(p,row('a','changed '+str(i)));m.index_file(c,p,cwd=str(tmp),rebuild=True);c.commit()
    assert c.execute('SELECT count(*) FROM memory_revisions WHERE id=? AND pinned=0',(old['block_id'],)).fetchone()[0]==3
    assert m.get_block(c,old['block_id'],revision=old['content_hash'])['text'].startswith('original')
    rev.pin(c,old['block_id'],old['content_hash'],enabled=False);c.commit()
    with pytest.raises(ValueError,match='Revision unavailable'):m.get_block(c,old['block_id'],revision=old['content_hash'])
    write(p,row('a','next'),row('b','later'));m.index_file(c,p,cwd=str(tmp),rebuild=True,max_records=1)
    c.execute("DELETE FROM memory_sources WHERE source_key='claude:rev'");c.commit()
    for table in ('memory_blocks','memory_chunks','memory_vectors','memory_revisions','memory_rebuild_blocks','memory_rebuild_segments'):
        assert c.execute('SELECT count(*) FROM '+table).fetchone()[0]==0


def test_hooks_stage_but_explicit_index_publishes_ready_rebuild(store):
    c,p,tmp=store;write(p,row('a','replacement'))
    r=m.index_file(c,p,cwd=str(tmp),rebuild=True,publish_rebuild=False);c.commit()
    assert r['state']=='rebuild_ready' and m.search(c,'original') and not m.search(c,'replacement')
    for _ in range(2):
        assert m.index_file(c,p,cwd=str(tmp),publish_rebuild=False)['state']=='rebuild_ready';c.commit()
    assert m.index_file(c,p,cwd=str(tmp))['state']=='complete';c.commit()
    assert m.search(c,'replacement')


def test_partial_record_and_interrupted_publication_do_not_expose_candidate(store,monkeypatch):
    c,p,tmp=store;write(p,row('a','candidate'))
    with p.open('a') as out:out.write('{"type":"user"')
    assert m.index_file(c,p,cwd=str(tmp),rebuild=True)['state']=='rebuilding';c.commit()
    assert m.search(c,'original') and not m.search(c,'candidate')
    with p.open('a') as out:out.write(',"uuid":"z","message":{"role":"user","content":"end"}}\n')
    original=m._put_block
    def fail(conn,record,staging=False):
        value=original(conn,record,staging)
        if not staging:raise RuntimeError('simulated publication interruption')
        return value
    with monkeypatch.context() as patch:
        patch.setattr(m,'_put_block',fail)
        with pytest.raises(RuntimeError):m.index_file(c,p,cwd=str(tmp))
    c.rollback()
    assert m.search(c,'original') and not m.search(c,'candidate')
    assert m.index_file(c,p,cwd=str(tmp))['state']=='complete';c.commit()
    assert m.search(c,'candidate')


def test_middle_of_previously_scanned_prefix_is_verified_before_publication(store):
    c,p,tmp=store;write(p,row('a','A'*700+' middle-marker '+'B'*700),row('b','last'))
    assert m.index_file(c,p,cwd=str(tmp),rebuild=True,max_records=1)['state']=='rebuilding';c.commit()
    p.write_text(p.read_text().replace('middle-marker','edited-marker'))
    r=m.index_file(c,p,cwd=str(tmp));c.commit()
    assert r['state']=='source_changed' and m.search(c,'original')
    assert not m.search(c,'last')
    assert m.index_file(c,p,cwd=str(tmp))['state']=='source_changed'
    assert m.index_file(c,p,cwd=str(tmp),rebuild=True)['state']=='complete';c.commit()
    assert m.search(c,'edited-marker')


def test_exports_exclude_candidates_and_optionally_roundtrip_pinned_history(store):
    c,p,tmp=store;old=m.search(c,'amber')[0];rev.pin(c,old['block_id'],old['content_hash'])
    write(p,row('a','replacement'));m.index_file(c,p,cwd=str(tmp),rebuild=True);c.commit()
    write(p,row('a','unpublished'),row('b','another'));m.index_file(c,p,cwd=str(tmp),rebuild=True,max_records=1);c.commit()
    data=export_source(c,'claude:rev',True)
    assert data['unpublished_rebuild_excluded'] and data['blocks'][0]['text']=='replacement'
    target=get_connection(tmp/'imported.db');result=import_source(target,data);target.commit()
    assert result['blocks_loaded']==1
    assert m.get_block(target,old['block_id'],revision=old['content_hash'])['text'].startswith('original')
    assert target.execute('SELECT pinned FROM memory_revisions WHERE content_hash=?',(old['content_hash'],)).fetchone()[0]==1
    assert not m.search(target,'unpublished');target.close()


def test_forged_export_hash_and_identity_are_rejected_before_target_write(store):
    c,p,tmp=store;data=export_source(c,'claude:rev',True);target=get_connection(tmp/'target.db')
    data['blocks'][0]['text']='tampered'
    with pytest.raises(ValueError,match='hash'):import_source(target,data)
    assert target.execute('SELECT count(*) FROM memory_sources').fetchone()[0]==0
    target.close()


def test_archive_text_cannot_be_overwritten_and_backup_restores_revisions(store):
    c,p,tmp=store;old=m.search(c,'amber')[0]
    write(p,row('a','changed'));m.index_file(c,p,cwd=str(tmp),rebuild=True);c.commit()
    with pytest.raises(sqlite3.IntegrityError,match='immutable'):
        c.execute("UPDATE memory_revisions SET text='changed archive'")
    c.rollback()
    target=get_connection(tmp/'copy.db');c.backup(target)
    assert target.execute('PRAGMA user_version').fetchone()[0]==SCHEMA_VERSION
    assert m.get_block(target,old['block_id'],revision=old['content_hash'])['text'].startswith('original')
    assert m.verify_schema(target)==[];target.close()


def test_doctor_detects_archive_corruption_even_with_required_objects_present(store):
    c,p,tmp=store;write(p,row('a','changed'));m.index_file(c,p,cwd=str(tmp),rebuild=True);c.commit()
    trigger=c.execute("SELECT sql FROM sqlite_master WHERE name='memory_revisions_immutable'").fetchone()[0]
    c.execute('DROP TRIGGER memory_revisions_immutable')
    c.execute("UPDATE memory_revisions SET text='corrupted archive'")
    c.execute(trigger);c.commit()
    assert 'memory_revisions contains invalid identity or content hash' in m.verify_schema(c)
    m.repair_schema(c)
    assert 'memory_revisions contains invalid identity or content hash' in m.verify_schema(c)


def test_schema_nine_migration_preserves_existing_text_and_fts(store):
    c,p,tmp=store;before=[tuple(r) for r in c.execute('SELECT id,text,content_hash FROM memory_blocks ORDER BY id')]
    for table in ('memory_rebuild_segments','memory_rebuild_blocks','memory_revisions','memory_revision_policy'):
        c.execute('DROP TABLE '+table)
    for col in ('rebuild_in_progress','rebuild_target','rebuild_base'):
        c.execute('ALTER TABLE memory_sources DROP COLUMN '+col)
    c.execute('PRAGMA user_version=9');c.commit();c.close()
    upgraded=get_connection(tmp/'store.db')
    assert before==[tuple(r) for r in upgraded.execute('SELECT id,text,content_hash FROM memory_blocks ORDER BY id')]
    assert m.search(upgraded,'amber') and m.verify_schema(upgraded)==[]
    upgraded.close()


def test_compaction_cites_retained_revision_after_later_edit(store):
    import re
    sys.path.insert(0,str(Path(__file__).parents[1]/'hooks'))
    from post_compact import build_recovery_context
    c,p,tmp=store;old=m.search(c,'amber')[0]
    context=build_recovery_context(c,'rev')
    assert f"get {old['block_id']} --start 0 --revision {old['content_hash']}" in context
    write(p,row('a','new text'));m.index_file(c,p,cwd=str(tmp),rebuild=True);c.commit()
    assert m.get_block(c,old['block_id'],revision=old['content_hash'])['text'].startswith('original amber')
