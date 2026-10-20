"""Astra round-two regressions (review of v2.4.0..2a4c21b, 2026-09-05), kept as the
regression suite for R01-R04 and R06-R11. Authored by Astra; adopted with one change:
R05 asserts the documented rebuild semantics chosen instead of the original guarantee
(see docs/update-window-plan.md P38). All paths and stores are temporary.
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path[:0]=[str(Path(__file__).parents[1]/'scripts'),str(Path(__file__).parents[1]/'hooks')]
import memory_store as memory
from db import get_connection
from recall_memory import parser,run


def rec(text,key,role='assistant',cwd=None):
    value={'type':role,'uuid':key,'timestamp':'2026-09-05T12:00:00Z',
           'sessionId':'round2','message':{'role':role,'content':[{'type':'text','text':text}]}}
    if cwd:value['cwd']=str(cwd)
    return value


def write(path,*records):
    path.write_text(''.join(json.dumps(r)+'\n' for r in records))


@pytest.fixture
def store(tmp_path):
    c=get_connection(tmp_path/'store.db')
    yield c,tmp_path
    c.close()


def scan(c,p,tmp,**kw):
    result=memory.index_file(c,p,session_id='round2',cwd=str(tmp),**kw)
    c.commit()
    return result


def test_restore_rejects_non_recall_sqlite_before_overwriting(store):
    c,tmp=store;p=tmp/'trace.jsonl';write(p,rec('irreplaceable evidence','a'))
    scan(c,p,tmp)
    bad=tmp/'other-application.db'
    other=sqlite3.connect(bad);other.execute('CREATE TABLE unrelated(value)');other.commit();other.close()
    try:
        run(parser().parse_args(['restore',str(bad),'--yes']),c)
    except (ValueError,sqlite3.Error):
        c.rollback()
    # A schema/integrity error must not destroy the original target database.
    table=c.execute("SELECT name FROM sqlite_master WHERE name='memory_blocks'").fetchone()
    assert table is not None,'restore replaced the target before validating it as a Recall backup'
    assert c.execute('SELECT text FROM memory_blocks').fetchone()[0]=='irreplaceable evidence'


def test_imported_generation_is_removed_by_first_rebuild(store):
    c,tmp=store;p=tmp/'trace.jsonl';write(p,rec('obsolete retained evidence','a'),rec('keep evidence','b'))
    scan(c,p,tmp)
    scan(c,p,tmp,rebuild=True)  # exported blocks now belong to generation 1
    export=run(parser().parse_args(['export','claude:round2']),c)
    path=tmp/'export.json';path.write_text(json.dumps(export))
    run(parser().parse_args(['prune','claude:round2']),c)
    run(parser().parse_args(['import-export',str(path)]),c);c.commit()
    write(p,rec('keep evidence','b'))
    assert scan(c,p,tmp,rebuild=True)['state']=='complete'
    assert not memory.search(c,'obsolete'),'exported block generation survives a completed replacement rebuild'


def test_rescope_persists_when_background_ingestion_resumes(store):
    c,tmp=store
    original=tmp/'original';corrected=tmp/'corrected';original.mkdir();corrected.mkdir()
    p=tmp/'trace.jsonl';write(p,rec('scoped evidence','a',cwd=original))
    memory.index_file(c,p);c.commit()
    result=run(parser().parse_args(['rescope','claude:round2','--cwd',str(corrected)]),c);c.commit()
    memory.index_file(c,p);c.commit()  # same call used by import_codex; trace keeps its original cwd
    actual=c.execute("SELECT repo_id FROM memory_sources WHERE source_key='claude:round2'").fetchone()[0]
    assert actual==result['now']['repo_id'],'automatic indexing silently reverted explicit rescope'


def test_rebuild_restores_source_order_for_inserted_earlier_messages(store):
    c,tmp=store;p=tmp/'trace.jsonl'
    write(p,rec('old opening','a','user'),rec('answer','b'));scan(c,p,tmp)
    write(p,rec('correct opening','new','user'),rec('old opening','a','user'),rec('answer','b'))
    scan(c,p,tmp,rebuild=True)
    ordered=[r[0] for r in c.execute('SELECT text FROM memory_blocks ORDER BY seq,ordinal')]
    assert ordered==['correct opening','old opening','answer']


def test_rebuild_resumes_after_transient_source_change_state(store):
    c,tmp=store;p=tmp/'trace.jsonl'
    write(p,rec('stale evidence','old'),rec('old second','old2'));scan(c,p,tmp)
    new=[rec('new first','n1'),rec('new second','n2')]
    write(p,*new);first=scan(c,p,tmp,rebuild=True,max_records=1)
    assert first['state']=='rebuilding'
    intended=p.read_bytes()
    p.write_bytes(b'')
    assert scan(c,p,tmp)['state']=='source_changed'
    p.write_bytes(intended)
    # Restoring the same source bytes lets the cursor continue; generation cleanup must also continue.
    assert scan(c,p,tmp)['state']=='complete'
    assert not memory.search(c,'stale'),'temporary source_changed erased the rebuild-in-progress flag'


def test_malformed_message_shape_does_not_wedge_durable_capture(store):
    c,tmp=store;p=tmp/'trace.jsonl'
    bad=rec('unused','bad');bad['message']['content']=7
    write(p,rec('before evidence','a'),bad,rec('after evidence','b'))
    try:
        scan(c,p,tmp)
    except (TypeError,AttributeError) as exc:
        c.rollback()
        pytest.fail('one valid JSON record wedges the whole durable pass: '+str(exc))
    assert memory.search(c,'after')


def test_source_filtered_search_coverage_matches_selected_source(store):
    c,tmp=store;p=tmp/'trace.jsonl';write(p,rec('evidence','a'));scan(c,p,tmp)
    result=run(parser().parse_args(['search','evidence','--source','claude:not-imported']),c)
    assert result['hits']==[]
    assert result['coverage']['source_count']==0,'global coverage is reported for an unindexed source'


def test_compaction_fetches_only_the_selected_source_text(store):
    from db import insert_session
    from post_compact import build_recovery_context
    c,tmp=store;p=tmp/'trace.jsonl'
    write(p,rec('opening','a','user'),*[rec('middle '*500,str(i)) for i in range(60)])
    scan(c,p,tmp);insert_session(c,'round2',str(tmp),'hash','2026-09-05')
    class MeasuredConnection:
        returned=0
        def execute(self,*args):
            cursor=c.execute(*args)
            outer=self
            class Cursor:
                def fetchall(self):
                    rows=cursor.fetchall();outer.returned+=sum(len(r['text']) for r in rows if 'text' in r.keys());return rows
                def fetchone(self):
                    row=cursor.fetchone()
                    if row is not None and 'text' in row.keys():outer.returned+=len(row['text'])
                    return row
            return Cursor()
    measured=MeasuredConnection()
    result=build_recovery_context(measured,'round2')
    assert len(result)<=3500
    assert measured.returned<=10000,'output is bounded, but every historical full text is materialized'


def test_partial_rebuild_keeps_published_history_until_atomic_replacement(store):
    """P52 supersedes the accepted R05 limitation: no partial replacement is exposed."""
    c,tmp=store;p=tmp/'trace.jsonl'
    write(p,rec('original accepted decision','same'),rec('former trailing block','later'));scan(c,p,tmp)
    write(p,rec('replacement proposal','same'),rec('subsequent trailing block','new'))
    assert scan(c,p,tmp,rebuild=True,max_records=1)['state']=='rebuilding'
    assert not memory.search(c,'replacement'),'replacement is staged until EOF'
    assert memory.search(c,'original'),'the complete published snapshot survives the rescan'
    assert memory.search(c,'former'),'not-yet-rescanned history stays until EOF'
    assert scan(c,p,tmp)['state']=='complete'
    assert not memory.search(c,'former') and memory.search(c,'subsequent')


def test_db_override_does_not_rename_default_home_legacy_index(tmp_path):
    import os,subprocess
    home=tmp_path/'home';legacy=home/'.claude/context-recall/index.json';legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({'session_id':'legacy-private','exchanges':[]}))
    scratch=tmp_path/'scratch';scratch.mkdir()
    env=dict(os.environ,HOME=str(home),RECALL_DB=str(scratch/'db'),RECALL_SETTINGS=str(scratch/'settings.json'),
             RECALL_LOG_FILE=str(scratch/'events.log'))
    env.pop('CLAUDE_ENV_FILE',None)
    root=Path(__file__).parents[1]
    result=subprocess.run([sys.executable,str(root/'hooks/prompt_submit.py')],
                          input=json.dumps({'session_id':'isolated','prompt':'hello','cwd':str(scratch)}),
                          env=env,capture_output=True,text=True,timeout=10)
    assert result.returncode==0
    assert legacy.exists(),'RECALL_DB redirected the store, but migration renamed the default HOME legacy file'


def test_redaction_whitespace_does_not_exceed_loose_process_budget(tmp_path):
    import os,subprocess
    root=Path(__file__).parents[1]
    code="import sys;sys.path.insert(0,sys.argv[1]);from utils import redact_secrets;redact_secrets('api_key'+' '*256000+'!')"
    try:
        result=subprocess.run([sys.executable,'-c',code,str(root/'scripts')],
                              capture_output=True,text=True,timeout=2,
                              env=dict(os.environ,HOME=str(tmp_path),RECALL_DB=str(tmp_path/'db'),
                                       RECALL_SETTINGS=str(tmp_path/'settings'),RECALL_LOG_FILE=str(tmp_path/'events')))
    except subprocess.TimeoutExpired:
        pytest.fail('generic credential delimiter still backtracks quadratically over whitespace (>2s)')
    assert result.returncode==0,result.stderr
