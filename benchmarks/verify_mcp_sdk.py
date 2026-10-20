#!/usr/bin/env python3
"""Independent MCP SDK compatibility probe on synthetic scratch data.

Developer-only dependency: uv run --with 'mcp==2.1.1' python benchmarks/verify_mcp_sdk.py
The Recall server itself imports only the standard library. No model is called.
"""
import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import signal
import subprocess
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]


async def probe(tmp):
    from mcp import Client, StdioServerParameters
    sys.path.insert(0,str(ROOT/'scripts'))
    from db import get_connection
    import memory_store as memory
    path=tmp/'shared?# memory.db'
    conn=get_connection(path)
    ids={}
    original={
        'claude': 'Accepted delivery label: amber-sparrow, because the legacy label collides. '+('Detailed evidence café. '*600),
        'codex': 'Rejected delivery label: violet, because it collides. The final build command was cargo test --release.',
        'outside': 'Private quasar evidence from another repository.'}
    traces={}
    for agent in original:
        source_agent='codex' if agent=='outside' else agent
        trace=tmp/(agent+'.jsonl'); traces[agent]=trace
        row=({'type':'assistant','uuid':agent,'message':{'role':'assistant','content':original[agent]}}
             if source_agent=='claude' else {'type':'response_item','payload':{'id':agent,'type':'message','role':'assistant',
                 'content':[{'type':'output_text','text':original[agent]}]}})
        trace.write_text(json.dumps(row)+'\n')
        memory.index_file(conn,trace,agent=source_agent,session_id=agent,cwd=str(tmp)); conn.commit()
        conn.execute('UPDATE memory_sources SET repo_id=?,scope_pinned=1 WHERE source_key=?',('hidden-repo' if agent=='outside' else 'shared-repo',source_agent+':'+agent)); conn.commit()
        ids[agent]=conn.execute('SELECT id FROM memory_blocks WHERE source_key=?',(source_agent+':'+agent,)).fetchone()[0]
    conn.close()
    before=hashlib.sha256(path.read_bytes()).hexdigest()
    server=StdioServerParameters(command=sys.executable,args=['-S',str(ROOT/'scripts/recall_mcp.py'),'--db',str(path),'--repo-id','shared-repo'],
                                env={'HOME':str(tmp),'RECALL_SETTINGS':str(tmp/'settings.json'),'RECALL_LOG_FILE':str(tmp/'events.log')})
    measurements=[]
    async with Client(server) as client:
        tools=await client.list_tools()
        assert {t.name for t in tools.tools}=={'recall_search','recall_get','recall_brief','recall_status'}
        async def call(name,args):
            start=time.monotonic(); r=await client.call_tool(name,args); measurements.append((time.monotonic()-start)*1000)
            assert not r.is_error,(name,r.content)
            return r.structured_content or json.loads(r.content[0].text)
        search=await call('recall_search',{'query':'delivery label'})
        assert {h['agent'] for h in search['hits']}=={'claude','codex'}
        for agent in ('claude','codex'):
            text=[]; start=0
            while True:
                block=await call('recall_get',{'block_id':ids[agent],'start':start,'max_chars':1900})
                assert block['start_char']==start
                text.append(block['text']); start=block['next_start']
                if start is None: break
            assert ''.join(text)==original[agent]
        status=await call('recall_status',{}); assert status['source_count']==2
        brief=await call('recall_brief',{}); assert 'hidden-repo' not in json.dumps(brief)
        absent=await call('recall_search',{'query':'unseen-absent-canary'}); assert not absent['hits']
        denied=await client.call_tool('recall_get',{'block_id':ids['outside']}); assert denied.is_error
        assert hashlib.sha256(path.read_bytes()).hexdigest()==before,'Read-only calls changed the store'
        # Same reader process must see a separately committed append from the capture adapter.
        with traces['codex'].open('a') as f: f.write(json.dumps({'type':'response_item','payload':{'id':'append','type':'message','role':'user',
                  'content':[{'type':'input_text','text':'Fresh appended marker cobalt-birch.'}]}})+'\n')
        capture=subprocess.Popen([sys.executable,'-S',str(ROOT/'scripts/recall_capture.py'),'--db',str(path),
                                  '--agent','codex','--path',str(traces['codex']),'--watch','--interval','0.05','--seconds','0.1'],
                                 env=dict(os.environ,HOME=str(tmp)),stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            for _ in range(30):
                await asyncio.sleep(0.02)
                fresh=await call('recall_search',{'query':'cobalt-birch'})
                if fresh['hits']: break
            else: raise AssertionError('Foreground watch did not make the append visible')
        finally:
            capture.send_signal(signal.SIGINT)
            capture_output,capture_error=capture.communicate(timeout=5)
        assert capture.returncode==0,capture_error
        assert any(json.loads(line)['blocks_updated'] for line in capture_output.splitlines())
        return {'sdk_version':importlib.metadata.version('mcp'),'protocol_version':client.protocol_version,
                'tool_names':[t.name for t in tools.tools], 'both_agents_retrieved':True,'exact_pagination':True,
                'cross_repo_get_denied':True,'scoped_status_and_brief':True,'absent_query_empty':True,
                'read_only_store_hash_unchanged':True,'later_capture_visible':True,
                'successful_tool_calls':len(measurements),'max_call_ms':round(max(measurements),2),
                'server_sha256':hashlib.sha256((ROOT/'scripts/recall_mcp.py').read_bytes()).hexdigest(),
                'foreground_watch_append_and_shutdown':True,'server_and_capture_site_packages_disabled':True,
                'model_called':False,'note':'Independent SDK transport/tool checks on synthetic evidence; not a coding-host model smoke test.'}


def main():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--out',type=Path); args=p.parse_args()
    with tempfile.TemporaryDirectory(prefix='recall-sdk-check-') as d:
        os.environ.update(HOME=d,RECALL_DB=d+'/unused.db',RECALL_SETTINGS=d+'/settings.json',RECALL_LOG_FILE=d+'/events.log')
        for k in ('CLAUDE_ENV_FILE','CLAUDE_CODE_SESSION_ID','RECALL_SESSION_ID','RECALL_PROJECT_HASH'): os.environ.pop(k,None)
        result=asyncio.run(asyncio.wait_for(probe(Path(d)),timeout=30))
    text=json.dumps(result,indent=2)+'\n'
    if args.out: args.out.write_text(text)
    print(text)


if __name__=='__main__': main()
