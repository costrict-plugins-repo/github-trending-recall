"""Exercise generated app packages through their actual launch configuration."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from zipfile import ZipFile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from prepare_claude_app import prepare
from test_recall_mcp import corpus


def launch(server, ids, cwd):
    messages = [
        {'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'app-package-test','version':'1'}}},
        {'jsonrpc':'2.0','method':'notifications/initialized'},
        {'jsonrpc':'2.0','id':2,'method':'tools/list'},
        {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'recall_status'}},
        {'jsonrpc':'2.0','id':4,'method':'tools/call','params':{'name':'recall_search','arguments':{'query':'decision'}}},
        {'jsonrpc':'2.0','id':5,'method':'tools/call','params':{'name':'recall_get','arguments':{'block_id':ids['accepted'],'max_chars':137}}},
        {'jsonrpc':'2.0','id':6,'method':'tools/call','params':{'name':'recall_get','arguments':{'block_id':ids['hidden']}}},
        {'jsonrpc':'2.0','id':7,'method':'tools/call','params':{'name':'recall_brief'}},
    ]
    result = subprocess.run([server['command'], *server['args']],
        input=''.join(json.dumps(m)+'\n' for m in messages), capture_output=True,
        text=True, timeout=10, cwd=cwd,
        env={'PATH':'/usr/bin:/bin','HOME':str(cwd),'RECALL_DB':str(cwd/'wrong.db')})
    assert result.returncode == 0, result.stderr
    replies = {m['id']:m for m in map(json.loads, result.stdout.splitlines())}
    assert len(replies[2]['result']['tools']) == 4
    assert replies[3]['result']['structuredContent']['source_count'] == 2
    assert {h['agent'] for h in replies[4]['result']['structuredContent']['hits']} == {'claude','codex'}
    assert replies[5]['result']['structuredContent']['next_start'] == 137
    assert replies[6]['result']['isError']
    assert 'Unknown block in this repository' in replies[6]['result']['content'][0]['text']
    assert not replies[7]['result']['isError']
    assert not (cwd/'wrong.db').exists()


def test_both_app_launch_paths_work_outside_repo_and_preserve_store(corpus, tmp_path):
    db, ids = corpus
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    out = tmp_path/'package ?# with space'
    receipt = prepare(out, db, 'repo-a', sys.executable)
    assert not receipt['installed'] and not receipt['contains_history']
    config = json.loads((out/'desktop-config-snippet.json').read_text())
    launch(config['mcpServers']['recall-reader'], ids, tmp_path)
    unpack = tmp_path/'separate app install'
    with ZipFile(out/'recall-reader.zip') as z:
        assert '.mcp.json' in z.namelist()
        assert '.claude-plugin/plugin.json' in z.namelist()
        assert not any(p.startswith('hooks/') or p.endswith('.db') for p in z.namelist())
        assert set(z.namelist()) == set(receipt['files'])
        z.extractall(unpack)
    server = json.loads((unpack/'.mcp.json').read_text())['mcpServers']['recall-reader-plugin']
    server['args'] = [arg.replace('${CLAUDE_PLUGIN_ROOT}', str(unpack)) for arg in server['args']]
    launch(server, ids, tmp_path)
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before


def test_preparation_refuses_empty_scope_missing_store_and_overwrite(corpus, tmp_path):
    db,_ = corpus
    out = tmp_path/'output'
    with pytest.raises(ValueError, match='No indexed sources'):
        prepare(out, db, 'not-indexed', sys.executable)
    assert not out.exists()
    with pytest.raises(FileNotFoundError):
        prepare(out, tmp_path/'missing.db', 'repo-a', sys.executable)
    assert not (tmp_path/'missing.db').exists()
    prepare(out, db, 'repo-a', sys.executable)
    sentinel = out/'keep.txt'; sentinel.write_text('user content')
    with pytest.raises(FileExistsError):
        prepare(out, db, 'repo-a', sys.executable)
    assert sentinel.read_text() == 'user content'


@pytest.mark.parametrize('name', ['../escape', 'Upper Case', 'x'*61, '${OTHER}', ''])
def test_plugin_name_cannot_escape_output(corpus, tmp_path, name):
    out = tmp_path/'output'
    with pytest.raises(ValueError, match='Name must'):
        prepare(out, corpus[0], 'repo-a', sys.executable, name)
    assert not out.exists()
