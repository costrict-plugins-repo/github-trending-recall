#!/usr/bin/env python3
"""Measure staged-rebuild overhead and explicit publication on a copied transcript.
Hooks never perform final promotion. Exit nonzero on stage/RSS or integrity failure.
Publication time/disk growth are measured maintenance costs, not hook guarantees.
"""
import argparse,json,resource,shutil,sys,tempfile,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import memory_store as m
from db import get_connection


def run(path):
    with tempfile.TemporaryDirectory(prefix='recall-revisions-') as d:
        tmp=Path(d);p=tmp/'trace.jsonl';shutil.copyfile(path,p);c=get_connection(tmp/'memory.db')
        def scan(rebuild=False,publish=True):
            times=[];start=time.monotonic()
            for iteration in range(100000):
                t=time.monotonic();r=m.index_file(c,p,agent='claude',session_id='budget',cwd=str(tmp),rebuild=rebuild and iteration==0,publish_rebuild=publish);c.commit();times.append(time.monotonic()-t)
                if r['state'] in ('complete','rebuild_ready'):break
                if r['state'] not in ('backlog','rebuilding'):raise RuntimeError(r)
            else:raise RuntimeError('scan failed to converge')
            return {'seconds':time.monotonic()-start,'max_pass_ms':max(times)*1000,'passes':len(times)}
        baseline=scan();c.execute('PRAGMA wal_checkpoint(TRUNCATE)');base_bytes=(tmp/'memory.db').stat().st_size
        before=c.execute('SELECT count(*) FROM memory_blocks').fetchone()[0]
        staged=scan(True,False);c.execute('PRAGMA wal_checkpoint(TRUNCATE)');stage_bytes=(tmp/'memory.db').stat().st_size
        assert c.execute('SELECT count(*) FROM memory_blocks').fetchone()[0]==before
        t=time.monotonic();r=m.index_file(c,p,agent='claude',session_id='budget',cwd=str(tmp));c.commit();promote=time.monotonic()-t
        assert r['state']=='complete' and c.execute('SELECT count(*) FROM memory_blocks').fetchone()[0]==before
        assert c.execute('SELECT count(*) FROM memory_revisions').fetchone()[0]==0
        errors=m.verify_schema(c);c.commit();c.execute('PRAGMA wal_checkpoint(TRUNCATE)');end_bytes=(tmp/'memory.db').stat().st_size
        peak=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/(1e6 if sys.platform=='darwin' else 1000)
        c.close()
        return {'transcript_bytes':p.stat().st_size,'published_blocks':before,'baseline':baseline,'staged':staged,
                'publication_seconds':promote,'baseline_db_bytes':base_bytes,'staged_db_bytes':stage_bytes,'final_db_bytes':end_bytes,
                'peak_rss_mb':peak,'schema_errors':errors,'over_budget':(['stage_pass'] if staged['max_pass_ms']>1000 else [])+(['rss'] if peak>100 else [])+(['schema'] if errors else []),
                'limits':'Unchanged rebuild; changed text/history grows by retained revisions. Freed SQLite pages remain reusable; no automatic vacuum. Final publication is explicit maintenance and is not constrained by the hook timeout.'}
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--transcript',type=Path,required=True);a=p.parse_args();r=run(a.transcript);print(json.dumps(r,indent=2));sys.exit(bool(r['over_budget']))
