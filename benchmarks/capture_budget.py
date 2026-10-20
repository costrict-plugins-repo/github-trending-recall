#!/usr/bin/env python3
"""P07 resource budget check for the capture hook, on a real transcript, in a temp store.

Copies TRANSCRIPT to a temp dir, backfills it through the same code path the
hooks use (prompt_submit.index_transcript, legacy exchanges + durable blocks),
then appends one small turn and times a steady-state pass. Never touches the
user's store: every env override points at the temp dir before any import.

    python3 benchmarks/capture_budget.py --transcript PATH [--label NAME] [--concurrent N] [--json]

Budgets (exit 1 if any is exceeded):
    steady-state pass <= 25 ms       any single pass <= 1000 ms (2 MB read)
    peak RSS <= 100 MB               database <= 0.5 x transcript bytes (no vectors)
--concurrent N runs N hook processes on N copies of the transcript against ONE
store at the same time and reports lock errors and row totals.
"""
import argparse, json, os, resource, shutil, subprocess, sys, tempfile, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUDGET = {'steady_state_pass_ms': 25.0, 'max_pass_ms': 1000.0, 'peak_rss_mb': 100.0, 'db_ratio': 0.5}


def _isolate(tmp):
    os.environ['HOME'] = tmp
    os.environ['RECALL_DB'] = os.path.join(tmp, 'unused-default.db')
    os.environ['RECALL_LOG_FILE'] = os.path.join(tmp, 'events.log')
    os.environ['RECALL_SETTINGS'] = os.path.join(tmp, 'settings.json')
    os.environ.pop('CLAUDE_CODE_SESSION_ID', None)


def _rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6


def measure(transcript, tmp, label):
    sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'hooks')]
    import prompt_submit as ps
    from db import get_connection, get_session
    tp = os.path.join(tmp, 't.jsonl'); shutil.copyfile(transcript, tp)
    db = os.path.join(tmp, 'r.db'); conn = get_connection(db)
    passes = []; t0 = time.perf_counter()
    while True:
        before = (get_session(conn, 'm') or {}).get('byte_offset', 0); t = time.perf_counter()
        ps.index_transcript(conn, 'm', tp, '/x', 'h'); conn.commit()
        passes.append(time.perf_counter() - t)
        if get_session(conn, 'm')['byte_offset'] <= before:
            break
    full = time.perf_counter() - t0
    with open(tp, 'a') as f:
        f.write(json.dumps({'type': 'user', 'timestamp': '2026-01-01T00:00:00Z', 'message': {'role': 'user', 'content': 'steady state prompt'}}) + '\n')
        f.write(json.dumps({'type': 'assistant', 'timestamp': '2026-01-01T00:00:01Z', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'steady state reply'}]}}) + '\n')
    t = time.perf_counter(); ps.index_transcript(conn, 'm', tp, '/x', 'h'); conn.commit(); steady = (time.perf_counter() - t) * 1000
    try:  # absent on revisions before the durable store (useful for baselines)
        durable = conn.execute("SELECT state, byte_offset FROM memory_sources WHERE source_key='claude:m'").fetchone()
    except Exception:
        durable = None
    conn.execute('PRAGMA wal_checkpoint(TRUNCATE)'); conn.close()
    import sqlite3
    c = sqlite3.connect(db); rows = {}
    for (n,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%_fts%' AND name NOT LIKE '%_config'"):
        rows[n] = c.execute(f'SELECT count(*) FROM "{n}"').fetchone()[0]
    c.close()
    size = os.path.getsize(db); tsize = os.path.getsize(transcript)
    result = {'label': label, 'transcript_mb': round(tsize / 1e6, 1), 'passes': len(passes),
              'full_backfill_s': round(full, 2), 'max_pass_ms': round(max(passes) * 1000, 1),
              'median_pass_ms': round(sorted(passes)[len(passes) // 2] * 1000, 1),
              'steady_state_pass_ms': round(steady, 1), 'peak_rss_mb': round(_rss_mb(), 1),
              'db_mb': round(size / 1e6, 2), 'db_ratio': round(size / tsize, 3),
              'durable_state': durable[0] if durable else None, 'rows': rows}
    result['over_budget'] = [k for k, lim in BUDGET.items() if result[k] > lim]
    return result


WORKER = '''
import json, os, sys, time
sys.path[:0] = [sys.argv[1] + '/scripts', sys.argv[1] + '/hooks']
import prompt_submit as ps
from db import get_connection
db, sid, tp = sys.argv[2:5]
conn = get_connection(db); errors = 0; passes = 0; t0 = time.perf_counter()
for _ in range(int(sys.argv[5])):
    try:
        ps.index_transcript(conn, sid, tp, '/x', 'h'); conn.commit(); passes += 1
    except Exception as exc:
        errors += 1; conn.rollback(); print(f'{sid}: {type(exc).__name__}: {exc}', file=sys.stderr)
print(json.dumps({'session': sid, 'passes': passes, 'errors': errors, 'seconds': round(time.perf_counter() - t0, 2)}))
'''


def concurrent(transcript, tmp, n, passes_each):
    db = os.path.join(tmp, 'shared.db'); procs = []
    for i in range(n):
        tp = os.path.join(tmp, f'c{i}.jsonl'); shutil.copyfile(transcript, tp)
        procs.append(subprocess.Popen([sys.executable, '-c', WORKER, str(ROOT), db, f'c{i}', tp, str(passes_each)],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=os.environ.copy()))
    outs = []
    for p in procs:
        out, err = p.communicate(timeout=600)
        lines = out.strip().splitlines()
        if not lines:
            raise RuntimeError('worker produced no result; stderr tail: ' + err.strip()[-800:])
        outs.append(json.loads(lines[-1]))
        if err.strip():
            outs[-1]['stderr'] = err.strip()[-300:]
    import sqlite3
    c = sqlite3.connect(db)
    totals = {'exchanges': c.execute('SELECT count(*) FROM exchanges').fetchone()[0],
              'blocks': c.execute('SELECT count(*) FROM memory_blocks').fetchone()[0],
              'sources': c.execute('SELECT count(*) FROM memory_sources').fetchone()[0],
              'integrity': c.execute('PRAGMA integrity_check').fetchone()[0]}
    c.close()
    return {'workers': outs, 'totals': totals, 'errors': sum(o['errors'] for o in outs)}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--transcript', required=True); ap.add_argument('--label', default='')
    ap.add_argument('--concurrent', type=int, default=0, help='also run N concurrent hook processes on one store')
    ap.add_argument('--passes-each', type=int, default=3); ap.add_argument('--json', action='store_true')
    a = ap.parse_args()
    tmp = tempfile.mkdtemp(prefix='recall-budget-'); _isolate(tmp)
    try:
        result = measure(a.transcript, tmp, a.label or Path(a.transcript).stem[:8])
        if a.concurrent:
            result['concurrent'] = concurrent(a.transcript, tmp, a.concurrent, a.passes_each)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if a.json:
        print(json.dumps(result, indent=1))
    else:
        r = result
        print(f"{r['label']}: {r['transcript_mb']} MB, {r['passes']} passes, backfill {r['full_backfill_s']} s, "
              f"max pass {r['max_pass_ms']} ms, median {r['median_pass_ms']} ms, steady {r['steady_state_pass_ms']} ms, "
              f"RSS {r['peak_rss_mb']} MB, db {r['db_mb']} MB ({r['db_ratio']}x), durable {r['durable_state']}, rows {r['rows']}")
        if 'concurrent' in r:
            c = r['concurrent']; print(f"concurrent x{len(c['workers'])}: errors {c['errors']}, totals {c['totals']}")
        print('over budget:', r['over_budget'] or 'none')
    return 1 if result['over_budget'] or result.get('concurrent', {}).get('errors') else 0


if __name__ == '__main__':
    sys.exit(main())
