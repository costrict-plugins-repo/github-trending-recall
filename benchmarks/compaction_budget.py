#!/usr/bin/env python3
"""P07: run both compact SessionStart commands in fresh interpreters.

Synthetic stores only. Includes startup, settings, database access, output and
deduplication. Tests ordinary history, large selected blocks, a writer lock and
opt-in Codex refresh. Gates: each hook sequence <= 2 s, hook peak RSS <= 100 MB.
These fixture gates are not universal bounds on unbounded user inputs or lock waits.
"""
import argparse
import json
import os
from pathlib import Path
import resource
import statistics
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
CASES = ('ordinary', 'long_history', 'large_angle', 'large_nul', 'writer_lock', 'codex_refresh')


def isolate(tmp):
    os.environ.update(HOME=tmp, RECALL_DB=tmp+'/store.db', RECALL_SETTINGS=tmp+'/settings.json',
                      RECALL_LOG_FILE=tmp+'/events.log', PYTHONDONTWRITEBYTECODE='1')
    for name in ('CLAUDE_ENV_FILE', 'CLAUDE_CODE_SESSION_ID', 'RECALL_SESSION_ID', 'RECALL_PROJECT_HASH'):
        os.environ.pop(name, None)
    os.environ['CLAUDE_ENV_FILE'] = tmp+'/session.env'
    os.environ['CLAUDE_PLUGIN_ROOT'] = str(ROOT)


def measure(case, tmp, trials):
    sys.path[:0] = [str(ROOT/'scripts'), str(ROOT/'hooks')]
    from db import get_connection, insert_session
    import memory_store as memory
    conn = get_connection()
    insert_session(conn, 'budget', tmp, 'fixture', '2026-09-06')
    conn.execute("INSERT INTO memory_sources(source_key,session_id,agent,path,project_path,repo_id,state) VALUES('claude:budget','budget','claude','synthetic',?,?,'complete')", (tmp,'fixture'))
    count = 10000 if case == 'long_history' else 1000
    retained = 0
    transcript_bytes = 0
    for i in range(count):
        text = 'Opening objective' if i == 0 else ('ordinary retained prose ' * 170 + 'FINAL_CONCLUSION')
        if i == count-1 and case in ('large_angle','large_nul'):
            text = ('<plain-code>' if case == 'large_angle' else 'prefix\x00') + 'x' * (8*1024*1024) + 'FINAL_CONCLUSION'
        retained += len(text.encode())
        transcript_bytes += len((json.dumps({'type':'user' if i == 0 else 'assistant','uuid':str(i),
                                             'message':{'role':'user' if i == 0 else 'assistant','content':text}})+'\n').encode())
        # Seed the complete retained representation, including FTS. Setup is not timed as a hook.
        memory._store_block(conn, 'claude:budget', str(i), i+1, 'user' if i == 0 else 'assistant',
                            'text', '2026-09-06', text, 0, 0, 0)
    conn.commit()
    conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    db_bytes = Path(os.environ['RECALL_DB']).stat().st_size
    conn.close()

    if case == 'codex_refresh':
        root = Path(tmp)/'rollouts'
        root.mkdir()
        path = root/'rollout.jsonl'
        path.write_text(json.dumps({'type':'session_meta','payload':{'id':'refresh','cwd':tmp}})+'\n'+
                        json.dumps({'type':'response_item','payload':{'type':'message','id':'a','role':'assistant',
                        'content':[{'type':'output_text','text':'REFRESH_FIRST'}]}})+'\n')
        Path(os.environ['RECALL_SETTINGS']).write_text(json.dumps({'codex_import':True,
                              'codex_sessions_dir':str(root),'codex_import_seconds':1.0}))

    payload = json.dumps({'session_id':'budget','cwd':tmp,'source':'compact'})
    manifest = json.loads((ROOT/'hooks/hooks.json').read_text())
    entries = manifest['hooks']['SessionStart']
    commands = {name: next(e['hooks'][0]['command'] for e in entries if e['matcher']==matcher)
                for name,matcher in [('session_start.py','*'),('post_compact.py','compact')]}
    durations, stages = [], []
    for trial in range(trials):
        # Clear the remembered injection before each independent trial.
        conn = get_connection()
        conn.execute("UPDATE sessions SET metadata=NULL WHERE session_id='budget'")
        conn.commit()
        conn.close()
        locked = threading.Event()
        def writer():
            import sqlite3
            lock = sqlite3.connect(os.environ['RECALL_DB'])
            lock.execute('BEGIN IMMEDIATE')
            locked.set()
            time.sleep(0.25)
            lock.rollback()
            lock.close()
        thread = None
        if case == 'writer_lock':
            thread = threading.Thread(target=writer)
            thread.start()
            locked.wait(timeout=2)
        timings = {}
        start = time.perf_counter()
        for hook in ('session_start.py', 'post_compact.py'):
            began = time.perf_counter()
            run = subprocess.run(['/bin/sh','-c',commands[hook]], input=payload, text=True,
                                 capture_output=True, env=dict(os.environ), timeout=8)
            timings[hook] = round(1000*(time.perf_counter()-began), 2)
            if run.returncode or run.stderr:
                raise RuntimeError(f'{hook} failed: {run.stderr[:500]}')
            output = json.loads(run.stdout)
        durations.append(1000*(time.perf_counter()-start))
        stages.append(timings)
        context = output['hookSpecificOutput']['additionalContext']
        assert len(context) <= 3500 and 'Opening objective' in context and 'FINAL_CONCLUSION' in context
        if thread:
            thread.join(timeout=2)

    again = subprocess.run(['/bin/sh','-c',commands['post_compact.py']],input=payload,text=True,
                           capture_output=True,env=dict(os.environ),timeout=8)
    assert not again.stderr and json.loads(again.stdout) == {}, 'Unchanged recovery must not repeat'
    refreshed = None
    if case == 'codex_refresh':
        with path.open('a') as out:
            out.write(json.dumps({'type':'response_item','payload':{'type':'message','id':'b','role':'assistant',
                       'content':[{'type':'output_text','text':'REFRESH_SECOND'}]}})+'\n')
        run = subprocess.run(['/bin/sh','-c',commands['session_start.py']], input=payload,text=True,
                             capture_output=True,env=dict(os.environ),timeout=8)
        assert not run.stderr and run.returncode == 0
        conn = get_connection()
        refreshed = bool(memory.search(conn,'REFRESH_SECOND',source_key='codex:refresh'))
        conn.close()
        assert refreshed, 'New Codex content must become searchable on the next Claude session start'
    rss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    rss_bytes = rss if sys.platform == 'darwin' else rss*1024
    return {'case':case,'blocks':count,'retained_utf8_bytes':retained,'database_bytes':db_bytes,
            'db_bytes_per_retained_byte':round(db_bytes/retained,3),
            'equivalent_jsonl_bytes':transcript_bytes,'db_to_equivalent_jsonl_ratio':round(db_bytes/transcript_bytes,3),
            'median_sequence_ms':round(statistics.median(durations),2),
            'max_sequence_ms':round(max(durations),2),'peak_hook_rss_mb':round(rss_bytes/1e6,2),
            'per_hook_ms':stages,'output_chars':len(context),'deduplicated':True,'codex_refresh':refreshed}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--case',choices=CASES)
    p.add_argument('--trials',type=int,default=3)
    p.add_argument('--max-ms',type=float,default=2000)
    p.add_argument('--max-rss-mb',type=float,default=100)
    args = p.parse_args()
    if args.trials < 1:
        p.error('trials must be positive')
    if args.case:
        with tempfile.TemporaryDirectory(prefix='recall-compact-budget-') as tmp:
            isolate(tmp)
            result = measure(args.case,tmp,args.trials)
        print(json.dumps(result))
        return 0
    results = []
    for case in CASES:
        child = subprocess.run([sys.executable,__file__,'--case',case,'--trials',str(args.trials)],
                               capture_output=True,text=True,timeout=120)
        if child.returncode:
            raise RuntimeError(f'{case}: {child.stderr[-1500:]}')
        result = json.loads(child.stdout)
        result['over_budget'] = []
        if result['max_sequence_ms'] > args.max_ms:
            result['over_budget'].append('wall_time')
        if result['peak_hook_rss_mb'] > args.max_rss_mb:
            result['over_budget'].append('peak_rss')
        results.append(result)
    print(json.dumps({'budgets':{'max_sequence_ms':args.max_ms,'peak_hook_rss_mb':args.max_rss_mb},
                      'scope':'Fresh processes, warm OS cache; fixture gates, not universal bounds. No live stores.',
                      'results':results},indent=2))
    return int(any(r['over_budget'] for r in results))


if __name__ == '__main__':
    sys.exit(main())
