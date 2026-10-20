#!/usr/bin/env python3
"""Measure Recall context sizes locally, without running the human evaluation.

Requires an explicitly supplied store and measurement-only tiktoken installation.
o200k_base counts are a reproducible proxy, not Claude billing. Nothing is sent to
a provider. The fixed operational probes below are not a retrieval-quality test.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'scripts'), str(ROOT/'hooks')]
from recall_mcp import Protocol, RecallService, tool_list
from post_compact import build_recovery_context
import tiktoken

PROBES = ('decision', 'test', 'benchmark', 'validation', 'capture', 'memory')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True, help='Frozen scratch snapshot; opened read-only')
    parser.add_argument('--repo-id', action='append', required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    encoder = tiktoken.get_encoding('o200k_base')
    def tokens(text): return len(encoder.encode(text, disallowed_special=()))
    def encoded(value): return json.dumps(value, ensure_ascii=False, allow_nan=False)
    before = hashlib.sha256(args.db.read_bytes()).hexdigest()
    conn = sqlite3.connect(args.db.resolve().as_uri()+'?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    result = {'tokenizer': encoder.name, 'tiktoken_version': tiktoken.__version__, 'snapshot_sha256': before,
              'model_requests': 0, 'probe_queries': list(PROBES), 'human_evaluation_used': False,
              'limitations': ['Token counts are o200k_base proxies, not Claude tokens or provider billing.',
                'MCP wire carries text and structuredContent; host serialization/deduplication determines what the model sees.',
                'Full retained-history comparison is a size reference, not demonstrated savings against a competent search workflow.',
                'Probe hits are not human-labelled, and top-hit reads are not scored for answer correctness.',
                'No model generation, repeated-context billing, cache prices or hidden host prompts are counted.'],
              'static': {'mcp_tool_schema_tokens': tokens(encoded(tool_list())),
                         'code_skill_tokens': tokens((ROOT/'skills/recall/SKILL.md').read_text())}, 'repositories': []}
    for repo in args.repo_id:
        service = RecallService(args.db, repo)
        protocol = Protocol(service)
        protocol.ready = True
        source_totals, total_chars, total_tokens = {}, 0, 0
        for row in conn.execute('SELECT b.source_key,b.text FROM memory_blocks b JOIN memory_sources s ON s.source_key=b.source_key WHERE s.repo_id=?', (repo,)):
            n = tokens(row['text'])
            total_chars += len(row['text']); total_tokens += n
            source_totals[row['source_key']] = source_totals.get(row['source_key'], 0)+n
        record = {'repo_id': repo, 'retained_chars': total_chars, 'sum_retained_block_tokens': total_tokens,
                  'sources': len(source_totals), 'probes': [], 'compaction_contexts': []}
        for query in PROBES:
            row = {'query': query}
            for limit in (1, 5):
                start = time.perf_counter()
                response = protocol.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'recall_search', 'arguments': {'query': query, 'limit': limit}}})
                duration = (time.perf_counter()-start)*1000
                envelope = response['result']
                if envelope['isError']:
                    row[str(limit)] = {'error': True}
                    continue
                value = envelope['structuredContent']
                text = envelope['content'][0]['text']
                measurement = {'hit_count': len(value['hits']), 'response_chars': len(text), 'single_content_tokens': tokens(text),
                               'wire_tokens': tokens(encoded(response)), 'coverage_tokens': tokens(encoded(value['coverage'])),
                               'latency_ms': round(duration, 3)}
                if value['hits']:
                    hit = value['hits'][0]
                    got = service.call('recall_get', {'block_id': hit['block_id'], 'start': hit['start_char'], 'max_chars': 2000, 'neighbors': 0})
                    measurement.update(get_2000_tokens=tokens(encoded(got)), source_retained_tokens=source_totals[hit['source_key']],
                        retrieved_text_tokens=tokens(got['text']), has_more=got['next_start'] is not None)
                row[str(limit)] = measurement
            record['probes'].append(row)
        record['status_tokens'] = tokens(encoded(service.call('recall_status', {})))
        record['brief_tokens'] = tokens(encoded(service.call('recall_brief', {})))
        for source in conn.execute("SELECT session_id FROM memory_sources WHERE repo_id=? AND agent='claude' ORDER BY session_id", (repo,)):
            context = build_recovery_context(conn, source['session_id'])
            if context:
                record['compaction_contexts'].append({'chars': len(context), 'tokens': tokens(context)})
        for limit in ('1', '5'):
            measurements = [r[limit] for r in record['probes'] if not r[limit].get('error')]
            record['limit_'+limit] = {'search_median_tokens': statistics.median(x['single_content_tokens'] for x in measurements),
                'search_max_tokens': max(x['single_content_tokens'] for x in measurements),
                'search_then_get_median_tokens': statistics.median(x['single_content_tokens']+x.get('get_2000_tokens', 0) for x in measurements)}
        result['repositories'].append(record)
    conn.close()
    assert hashlib.sha256(args.db.read_bytes()).hexdigest() == before
    result['snapshot_unchanged'] = True
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps({'static': result['static'], 'repositories': [{k:v for k,v in r.items() if k not in ('probes', 'compaction_contexts')} for r in result['repositories']]}))


if __name__ == '__main__':
    main()
