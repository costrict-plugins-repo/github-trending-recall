#!/usr/bin/env python3
"""Private-corpus anchor evaluation. Prints metrics/IDs, never transcript text.

python3 benchmarks/evaluate.py --trace PATH --agent claude [--model-path LOCAL_DIR]
The first 40 questions form a development group; the last 20 are reserved.
See README.md: both groups were inspected during development, so neither is
an untouched holdout. This script does not call a generative model.
"""
import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from db import get_connection, search_exchanges_fts
import memory_store as memory
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'hooks'))
import prompt_submit as legacy


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--trace',type=Path,required=True)
    p.add_argument('--agent',choices=['claude','codex'],default='claude')
    p.add_argument('--cases',type=Path,default=Path(__file__).with_name('recall_questions.json'))
    p.add_argument('--model-path',type=Path)
    args=p.parse_args()
    cases=json.loads(args.cases.read_text())['cases']
    with tempfile.TemporaryDirectory(prefix='recall-evaluation-') as temp:
        # Freeze the trace so a running session cannot change the corpus mid-run.
        trace=Path(temp)/args.trace.name;trace.write_bytes(args.trace.read_bytes())
        conn=get_connection(Path(temp)/'eval.db')
        while True:
            result=memory.index_file(conn,trace,args.agent)
            conn.commit()
            if result['state']!='backlog':break
        corpus=[dict(r) for r in conn.execute('SELECT id,text,role FROM memory_blocks WHERE kind=\'text\'')]
        # v2.4 legacy arm: the capped exchange store built from the SAME snapshot.
        legacy.MAX_BYTES_PER_READ=10**12; legacy.MAX_MESSAGES_PER_READ=10**9
        legacy.index_transcript(conn,'legacy-eval',str(trace),'/eval','h'); conn.commit()
        if args.model_path:
            from semantic_memory import build,hybrid_search
            start=time.perf_counter();built=build(conn,args.model_path)
            print(json.dumps({'semantic_build':built,'seconds':time.perf_counter()-start}),flush=True)
        measurements=[]
        for index,case in enumerate(cases):
            gold={r['id'] for r in corpus if case['anchor'].lower() in r['text'].lower() and r['role']=='assistant'}
            row={'case':index+1,'split':'development' if index<40 else 'held_out',
                 'query':case['query'],'anchor':case['anchor'],'gold_blocks':len(gold)}
            start=time.perf_counter();hits=memory.search(conn,case['query'],limit=30)
            row['lexical_ms']=(time.perf_counter()-start)*1000
            row['lexical_hit5']=bool(gold.intersection(r['block_id'] for r in hits[:5])) if gold else None
            row['lexical_chars']=sum(len(r['text']) for r in hits[:5])
            # legacy hit: the anchor appears in any text of the top-5 capped exchanges
            start=time.perf_counter();lhits=search_exchanges_fts(conn,case['query'],session_id='legacy-eval',limit=5)
            row['legacy_ms']=(time.perf_counter()-start)*1000
            row['legacy_hit5']=any(case['anchor'].lower() in ((h.get('user_text') or '')+(h.get('assistant_text') or '')+(h.get('tool_text') or '')).lower() for h in lhits) if gold else None
            row['legacy_chars']=sum(len(h.get('user_text') or '')+len(h.get('assistant_text') or '') for h in lhits)
            if args.model_path:
                start=time.perf_counter();hybrid=hybrid_search(conn,case['query'],hits,5)
                row['hybrid_ms']=(time.perf_counter()-start)*1000+row['lexical_ms']
                row['hybrid_hit5']=bool(gold.intersection(r['block_id'] for r in hybrid)) if gold else None
                row['hybrid_chars']=sum(len(r['text']) for r in hybrid)
            measurements.append(row)
        summaries={}
        for split in ('development','held_out'):
            subset=[r for r in measurements if r['split']==split]
            if not subset:
                continue
            summaries[split]={'questions':len(subset),'labelled':sum(r['gold_blocks']>0 for r in subset)}
            for arm in (('legacy','lexical','hybrid') if args.model_path else ('legacy','lexical')):
                valid=[r for r in subset if r.get(arm+'_hit5') is not None]
                latency=sorted(r[arm+'_ms'] for r in subset)
                summaries[split][arm]={'hit_at_5':sum(r[arm+'_hit5'] for r in valid)/len(valid) if valid else None,
                    'median_ms':statistics.median(latency),'p95_ms':latency[min(len(latency)-1,int(len(latency)*.95))],
                    'mean_passage_chars':statistics.mean(r[arm+'_chars'] for r in subset)}
        print(json.dumps({'method':'assistant-text anchor labels; not a human-judged answer quality benchmark',
                          'summary':summaries,'cases':measurements},indent=2))
        conn.close()


if __name__=='__main__':main()
