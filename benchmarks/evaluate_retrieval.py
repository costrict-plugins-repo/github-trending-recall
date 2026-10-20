#!/usr/bin/env python3
"""Reproduce the source-grounded 2.6 synthetic retrieval checks in a scratch store.

No answer model is called. Optional --model-path encodes with an existing local
model; no download. This fixture is now public regression data, not a new blind
holdout. Gold labels are used only for validation/scoring, never query rewriting.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from db import get_connection
import memory_store as memory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture', type=Path, required=True)
    parser.add_argument('--model-path')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    raw = args.fixture.read_bytes()
    if hashlib.sha256(raw).hexdigest() != args.fixture.with_suffix('.sha256').read_text().strip():
        parser.error('Fixture hash mismatch')
    if args.out.exists():
        parser.error('Refusing to overwrite a prior result')
    data = json.loads(raw)
    with tempfile.TemporaryDirectory(prefix='recall-retrieval-') as tmp:
        root = Path(tmp)
        conn = get_connection(root / 'store.db')
        try:
            expected = {}
            for session in data['sessions']:
                records = []
                for message in session['messages']:
                    expected[message['id']] = message['text']
                    if session['agent'] == 'claude':
                        records.append({'type': message['role'], 'uuid': message['id'],
                            'timestamp': session['timestamp'], 'message': {'role': message['role'], 'content': message['text']}})
                    else:
                        records.append({'type': 'response_item', 'timestamp': session['timestamp'],
                            'payload': {'type': 'message', 'id': message['id'], 'role': message['role'],
                            'content': [{'type': 'input_text' if message['role'] == 'user' else 'output_text', 'text': message['text']}]}})
                path = root / (session['session_id'] + '.jsonl')
                path.write_text(''.join(json.dumps(r) + '\n' for r in records))
                memory.index_file(conn, path, agent=session['agent'], session_id=session['session_id'], cwd=str(root))
            conn.commit()
            actual = dict(conn.execute('SELECT message_key,text FROM memory_blocks'))
            if actual != expected:
                raise RuntimeError('Ingested messages differ from the canonical source')
            if args.model_path:
                from semantic_memory import build, hybrid_search
                build(conn, args.model_path)
            rows = []
            for case in data['cases']:
                if not set(case['evidence_ids']) <= set(expected):
                    raise ValueError('Unknown source label in ' + case['id'])
                start = time.monotonic()
                hits = memory.search(conn, case['question'], limit=30 if args.model_path else 5)
                if args.model_path:
                    hits = hybrid_search(conn, case['question'], hits, limit=5)
                elapsed = time.monotonic() - start
                ids = [conn.execute('SELECT message_key FROM memory_blocks WHERE id=?', (h['block_id'],)).fetchone()[0] for h in hits]
                rows.append({'id': case['id'], 'all_evidence': set(case['evidence_ids']) <= set(ids),
                             'returned_message_ids': ids, 'seconds': elapsed})
        finally:
            conn.close()
    report = {'fixture_sha256': hashlib.sha256(raw).hexdigest(), 'cases': len(rows),
              'exact_support_cases': sum(r['all_evidence'] for r in rows), 'semantic': bool(args.model_path), 'rows': rows,
              'note': 'Required source-record coverage at five; not generated-answer accuracy or independent human judging.'}
    args.out.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({k: v for k, v in report.items() if k != 'rows'}))
    return 0 if all(r['all_evidence'] for r in rows) else 1


if __name__ == '__main__':
    sys.exit(main())
