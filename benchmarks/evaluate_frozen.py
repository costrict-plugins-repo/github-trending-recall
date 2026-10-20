#!/usr/bin/env python3
"""Run the three retrieval arms once over a frozen, source-grounded question set (P10).

    python3 benchmarks/evaluate_frozen.py --fixture questions.json [--model-path DIR] [--out results.json]

Protocol (from the fixture's README): verify the fixture hash; snapshot each source's
frozen byte prefix read-only and verify its hash; index the snapshots into one temp store
(durable blocks + legacy exchanges, the same code the hooks run); run every arm with the
same top-5 budget (legacy supports only Claude sources), both unscoped (every repository) and scoped to the question's source
repository; score positives against the labelled message/ordinal (exact) and, separately,
against the anchor text (broad). False-premise cases are listed, never scored. No tuning.
Use --validate-only to check labels without retrieval. Human-review packets require
all cases accepted plus an actual human_review_receipt before running retrieval.
"""
import argparse, hashlib, json, os, shutil, sys, tempfile, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'hooks')]


def sha256(path, limit=None):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        remaining = limit
        while True:
            chunk = f.read(min(1 << 20, remaining) if remaining is not None else 1 << 20)
            if not chunk:
                break
            h.update(chunk)
            if remaining is not None:
                remaining -= len(chunk)
                if remaining <= 0:
                    break
    return h.hexdigest()


def case_key(case):
    """Claude uuid labels and Codex payload-id/byte-offset labels use one store key."""
    key = case.get('message_key', case.get('message_id'))
    if key is None:
        raise ValueError(f"{case['id']}: missing exact message label")
    return str(key)


def validate_case_labels(conn, fixture, snapshots):
    """Resolve source-line labels before searching; an anchor is never the gold label."""
    import memory_store as memory
    sources = {(s['agent'], s['session_id']): (name, s) for name, s in fixture['sources'].items()}
    labels = {}
    records = {}
    for name, path in snapshots.items():
        offset = 0
        records[name] = []
        for line in path.read_bytes().splitlines(keepends=True):
            records[name].append((offset, line))
            offset += len(line)
    for case in fixture['cases']:
        name, src = sources[(case['agent'], case['session_id'])]
        if case['transcript'] != src['transcript']:
            raise ValueError(f"{case['id']}: transcript disagrees with source manifest")
        if not case.get('answer_present', True):
            continue
        try:
            line_number = case['source_line']
            if not isinstance(line_number, int) or line_number < 1:
                raise ValueError('invalid source line')
            start, raw = records[name][line_number - 1]
            entry = json.loads(raw)
            payload = entry.get('payload') or {}
            key = str(entry.get('uuid') or payload.get('id') or payload.get('call_id') or start)
            if key != case_key(case):
                raise ValueError('source line and message key disagree')
            if 'start_byte' in case and case['start_byte'] != start:
                raise ValueError('source byte offset disagrees')
            ordinal = case['block_ordinal']
            if not any(kind == 'text' and n == ordinal for _, kind, _, n in memory.normalize_record(entry, src['agent'])):
                raise ValueError('label is not a retained text block')
            label = memory.digest(f"{src['agent']}:{case['session_id']}:{key}:{ordinal}:text")[:32]
            if not conn.execute('SELECT 1 FROM memory_blocks WHERE id=?', (label,)).fetchone():
                raise ValueError('label missing from indexed snapshot')
            labels[case['id']] = label
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ValueError(f"{case['id']}: invalid exact label: {exc}") from exc
    return labels


def review_pending(fixture):
    return [c['id'] for c in fixture['cases'] if c.get('review_status') != 'accepted']


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--fixture', required=True, type=Path)
    ap.add_argument('--model-path', help='local sentence-transformers model dir for the hybrid arm')
    ap.add_argument('--out', type=Path)
    ap.add_argument('--limit', type=int, default=5)
    ap.add_argument('--validate-only', action='store_true', help='Verify snapshot hashes and exact labels without retrieval; pending human review is allowed')
    a = ap.parse_args()
    fixture = json.loads(a.fixture.read_text())
    expected = (a.fixture.with_suffix('.sha256').read_text().split() or [''])[0]
    actual = sha256(a.fixture)
    if expected != actual:
        sys.exit(f'fixture hash mismatch: {actual} != {expected}')
    if a.limit < 1:
        sys.exit('limit must be positive')
    if not fixture['cases'] or len({c['id'] for c in fixture['cases']}) != len(fixture['cases']):
        sys.exit('case IDs must be nonempty and unique')
    is_human_packet = fixture.get('format') == 'recall-human-review-candidates-v1'
    pending = review_pending(fixture)
    if is_human_packet and not a.validate_only and (pending or not fixture.get('human_review_receipt')):
        sys.exit('Human review is pending: accept/correct every case and record the actual human review receipt before retrieval.')
    tmp = tempfile.mkdtemp(prefix='recall-frozen-')
    os.environ.update(HOME=tmp, RECALL_DB=tmp + '/unused.db', RECALL_LOG_FILE=tmp + '/e.log', RECALL_SETTINGS=tmp + '/s.json')
    for key in ('CLAUDE_CODE_SESSION_ID', 'RECALL_SESSION_ID', 'RECALL_PROJECT_HASH', 'CLAUDE_ENV_FILE'):
        os.environ.pop(key, None)
    import memory_store as memory
    import prompt_submit as legacy
    from db import get_connection, get_session, search_exchanges_global, search_exchanges_fts, get_exchanges
    report = {'fixture': str(a.fixture), 'fixture_sha256': actual, 'review_base': fixture.get('review_base'),
              'runner_sha256': sha256(Path(__file__)),
              'runtime_sha256': {str(p.relative_to(ROOT)): sha256(p) for folder in ('scripts', 'hooks') for p in sorted((ROOT / folder).glob('*.py'))},
              'method': 'labelled message/ordinal (exact) and anchor substring (broad); top-%d; no tuning' % a.limit,
              'sources': {}, 'arms': {}, 'cases': []}
    try:
        conn = get_connection(Path(tmp) / 'eval.db')
        snapshots = {}
        for name, src in fixture['sources'].items():
            path = src['transcript']; n = src['frozen_bytes']
            snap = Path(tmp) / f'{name}.jsonl'
            with open(path, 'rb') as f, open(snap, 'wb') as g:
                g.write(f.read(n))
            got = sha256(snap)
            if snap.stat().st_size != n or got != src['sha256']:
                raise ValueError(f'{name}: frozen source prefix hash/length mismatch; refusing evaluation')
            snapshots[name] = snap
            report['sources'][name] = {'frozen_bytes': n, 'sha256_ok': True, 'live_bytes': os.path.getsize(path)}
            # durable
            t = time.perf_counter()
            while True:
                r = memory.index_file(conn, snap, agent=src['agent'], session_id=src['session_id'])
                conn.commit()
                if r['state'] == 'complete':
                    break
                if r['state'] != 'backlog':
                    raise ValueError(f'{name}: incomplete import: {r["state"]}')
            sid = src['session_id']
            row = conn.execute('SELECT repo_id,project_path FROM memory_sources WHERE source_key=?', (f"{src['agent']}:{sid}",)).fetchone()
            # The legacy parser has no Codex adapter. Do not turn unsupported input into a zero score.
            if src['agent'] == 'claude':
                while True:
                    before = (get_session(conn, sid) or {}).get('byte_offset', 0)
                    legacy.index_transcript(conn, sid, str(snap), project_path=row[1], project_hash=row[0])
                    conn.commit()
                    if get_session(conn, sid)['byte_offset'] <= before:
                        break
            report['sources'][name]['repo_id'] = row[0] if row else None
            report['sources'][name]['blocks'] = conn.execute('SELECT count(*) FROM memory_blocks WHERE source_key=?', (f"{src['agent']}:{sid}",)).fetchone()[0]
            report['sources'][name]['exchanges'] = conn.execute('SELECT count(*) FROM exchanges WHERE session_id=?', (sid,)).fetchone()[0]
            report['sources'][name]['index_seconds'] = round(time.perf_counter() - t, 2)
        labels = validate_case_labels(conn, fixture, snapshots)
        if a.validate_only:
            text = json.dumps({'validation': 'passed', 'fixture_sha256': actual, 'sources': report['sources'],
                               'exact_labels': len(labels), 'cases': len(fixture['cases']),
                               'human_pending': len(pending), 'retrieval_runs': 0}, indent=2)
            if a.out:
                a.out.write_text(text + '\n')
            print(text)
            return
        arms = ['legacy', 'lexical']
        if a.model_path:
            from semantic_memory import build, hybrid_search
            t = time.perf_counter()
            built = build(conn, a.model_path)
            report['semantic_build'] = dict(built, seconds=round(time.perf_counter() - t, 1))
            report['model_path'] = built['model_path']
            report['model_fingerprint'] = built['model_fingerprint']
            arms.append('hybrid')
        by_session = {(s['agent'], s['session_id']): (k, s) for k, s in fixture['sources'].items()}

        def run_arm(arm, case, scoped):
            q = case['question']; name, src = by_session[(case['agent'], case['session_id'])]
            if arm == 'legacy' and src['agent'] != 'claude':
                return {'supported': False, 'reason': 'Legacy capture has no Codex adapter', 'top_ids': []}
            repo = report['sources'][name]['repo_id'] if scoped else None
            t = time.perf_counter()
            if arm == 'legacy':
                if scoped:
                    rows = search_exchanges_fts(conn, q, project_hash=repo, limit=a.limit)
                else:
                    rows = search_exchanges_global(conn, q, limit=a.limit)
                hits = [{'session_id': r['session_id'], 'text': (r.get('user_text') or '') + '\n' + (r.get('assistant_text') or ''),
                         'block_id': None} for r in rows]
            else:
                lexical = memory.search(conn, q, max(30, a.limit) if arm == 'hybrid' else a.limit, repo)
                if arm == 'hybrid':
                    lexical = hybrid_search(conn, q, lexical, a.limit, repo)
                hits = [{'session_id': h.get('session_id'), 'block_id': h.get('block_id'), 'text': h.get('text') or h.get('snippet') or ''} for h in lexical[:a.limit]]
            ms = (time.perf_counter() - t) * 1000
            label = labels.get(case['id'])
            exact = any(h['block_id'] == label for h in hits) if label and arm != 'legacy' else None
            anchor = case.get('anchor')
            broad = any(anchor in h['text'] for h in hits) if anchor else None
            right_session = any(h['session_id'] == case['session_id'] for h in hits)
            return {'supported': True, 'exact': exact, 'broad': broad, 'right_session': right_session, 'ms': round(ms, 1),
                    'context_chars': sum(len(h['text']) for h in hits), 'top_ids': [h['block_id'] for h in hits]}

        for case in fixture['cases']:
            entry = {'id': case['id'], 'answer_present': case.get('answer_present', True), 'category': case.get('category'), 'agent': case['agent'], 'results': {}}
            for arm in arms:
                for scope in ('unscoped', 'scoped'):
                    entry['results'][f'{arm}/{scope}'] = run_arm(arm, case, scope == 'scoped')
            report['cases'].append(entry)
        positives = [c for c in report['cases'] if c['answer_present']]
        for arm in arms:
            for scope in ('unscoped', 'scoped'):
                k = f'{arm}/{scope}'; rs = [c['results'][k] for c in positives if c['results'][k]['supported']]
                if not rs:
                    report['arms'][k] = {'positives': 0, 'unsupported': len(positives)}
                    continue
                exact = [r['exact'] for r in rs if r['exact'] is not None]
                broad = [r['broad'] for r in rs if r['broad'] is not None]
                report['arms'][k] = {'positives': len(rs), 'unsupported': len(positives) - len(rs),
                                     'exact_labelled': len(exact), 'anchor_labelled': len(broad),
                                     'exact_hit_at_%d' % a.limit: round(sum(exact) / len(exact), 3) if exact else None,
                                     'broad_hit_at_%d' % a.limit: round(sum(broad) / len(broad), 3) if broad else None,
                                     'right_session_rate': round(sum(r['right_session'] for r in rs) / len(rs), 3),
                                     'median_ms': round(sorted(r['ms'] for r in rs)[len(rs) // 2], 1),
                                     'mean_context_chars': round(sum(r['context_chars'] for r in rs) / len(rs))}
        report['false_premise_cases'] = [{'id': c['id'], 'returned': {k: v['top_ids'] for k, v in c['results'].items()}}
                                         for c in report['cases'] if not c['answer_present']]
        report['human_review_receipt'] = fixture.get('human_review_receipt')
        report['answer_quality_scored'] = False
        report['note'] = ('Legacy has no message ids, so its exact score is null; compare broad and right-session rates. '
                          'Legacy is evaluated only for Claude cases; its denominator and supported corpus differ. '
                          'Scores are correlated; label review is distinct from judging generated answers. '
                          'See human_review_receipt for label-review provenance; absence means not human-reviewed.')
    finally:
        if 'conn' in locals():
            conn.close()
        shutil.rmtree(tmp, ignore_errors=True)
    text = json.dumps(report, indent=1)
    if a.out:
        a.out.write_text(text)
    print(json.dumps({'sources': report['sources'], 'arms': report['arms'], 'false_premise_cases': report['false_premise_cases']}, indent=1))


if __name__ == '__main__':
    main()
