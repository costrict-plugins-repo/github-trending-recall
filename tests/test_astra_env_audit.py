"""Positive controls for all non-store path override routes; no real HOME is used."""
import json,os,subprocess,sys
from pathlib import Path
import pytest
ROOT=Path(__file__).parents[1]


@pytest.mark.parametrize('operation',['settings','events','codex','skill'])
def test_non_store_overrides_reach_only_requested_paths(tmp_path,operation):
    home=tmp_path/'home';home.mkdir();scratch=tmp_path/'scratch';scratch.mkdir()
    sentinel=home/'sentinel';sentinel.write_text('unchanged')
    env=dict(os.environ,HOME=str(home),RECALL_DB=str(scratch/'db'),RECALL_SETTINGS=str(scratch/'settings.json'),
             RECALL_LOG_FILE=str(scratch/'events.log'),CLAUDE_ENV_FILE=str(scratch/'claude-env'))
    for k in ('CLAUDE_CODE_SESSION_ID','RECALL_SESSION_ID','RECALL_PROJECT_HASH'):env.pop(k,None)
    def run(args,payload=''):
        r=subprocess.run([sys.executable,*args],cwd=ROOT,env=env,input=payload,capture_output=True,text=True,timeout=10)
        assert r.returncode==0,(r.stdout,r.stderr)
        return json.loads(r.stdout)
    if operation=='settings':
        data=run(['scripts/recall_memory.py','config','codex_import','off'])
        assert Path(data['path'])==scratch/'settings.json'
        assert json.loads((scratch/'settings.json').read_text())['codex_import'] is False
    elif operation=='events':
        run(['hooks/prompt_submit.py'],json.dumps({'session_id':'audit','prompt':'/recall','cwd':str(scratch)}))
        assert 'session=audit' in (scratch/'events.log').read_text()
    elif operation=='codex':
        sessions=scratch/'rollouts';sessions.mkdir()
        tr=sessions/'audit.jsonl'
        tr.write_text(json.dumps({'type':'session_meta','payload':{'id':'audit','cwd':str(scratch)}})+'\n'+
                      json.dumps({'type':'response_item','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':'chosen directory evidence'}]}})+'\n')
        (scratch/'settings.json').write_text(json.dumps({'codex_import':True,'codex_sessions_dir':str(sessions)}))
        run(['hooks/session_start.py'],json.dumps({'session_id':'audit','cwd':str(scratch)}))
        assert (scratch/'claude-env').is_file()
        result=run(['scripts/recall_memory.py','search','chosen directory','--all'])
        assert result['hits'][0]['source_key']=='codex:audit'
    else:
        dest=scratch/'skills'
        result=run(['scripts/recall_memory.py','install-codex-skill','--skills-dir',str(dest)])
        assert Path(result['written'])==dest/'recall/SKILL.md'
        assert str(ROOT/'scripts/recall_memory.py') in Path(result['written']).read_text()
    assert sorted(str(p.relative_to(home)) for p in home.rglob('*') if p.is_file())==['sentinel']
    assert sentinel.read_text()=='unchanged'
