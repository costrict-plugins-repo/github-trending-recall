"""Candidate ranking regressions; source identity and excerpt offsets stay intact."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
from retrieval_ranking import fuse


def hit(key, text='', start=0):
    return {'block_id': key, 'text': text or key, 'start_char': start,
            'content_hash': 'retained-' + key}


def test_strong_semantic_only_hit_survives_overlapping_noise():
    lexical = [hit('n' + str(i)) for i in range(30)]
    semantic = [hit('answer')] + lexical
    result = fuse(lexical, semantic, 5)
    assert 'answer' in [r['block_id'] for r in result[:2]]
    assert len(result) == 5


def test_exact_lexical_leader_survives_semantic_noise():
    semantic = [hit('n' + str(i)) for i in range(30)]
    lexical = [hit('exact-command')] + semantic
    assert 'exact-command' in [r['block_id'] for r in fuse(lexical, semantic, 5)[:2]]


def test_semantic_winning_excerpt_not_unrelated_chunk_of_same_block():
    lexical = [hit('noise'), hit('long-block', 'unrelated opening', 0)]
    semantic = [hit('long-block', 'required conclusion', 4200)]
    result = next(r for r in fuse(lexical, semantic, 5) if r['block_id'] == 'long-block')
    assert result['text'] == 'required conclusion'
    assert result['start_char'] == 4200
    assert result['content_hash'] == 'retained-long-block'


def test_duplicate_chunks_never_cast_extra_votes():
    result = fuse([hit('a'), hit('a'), hit('b')], [hit('c'), hit('a'), hit('a')], 10)
    assert len(result) == 3
    assert {r['block_id'] for r in result[:2]} == {'a', 'c'}


def test_empty_channel_and_zero_limit():
    assert fuse([], [hit('a')], 2)[0]['block_id'] == 'a'
    assert fuse([hit('a')], [], 0) == []
    assert fuse([], [], 3) == []


def test_question_capitalization_does_not_promote_status_over_explanation(tmp_path):
    import json
    from db import get_connection
    import memory_store as memory
    messages = [
        ('ask', 'Why did the Recall capture hook time out on the big transcript?'),
        ('answer', 'The hook timed out because the incremental reader re-read the same chunk forever when the transcript exceeded the byte budget; we capped incremental reads at 2 MB per pass to avoid the wedge.'),
        ('status', 'Status update for Recall and Claude and Python today?'),
        ('noise', 'Recall, Claude and Python are all fine today. Nothing to report about the Recall hook, Claude sessions, or Python versions.'),
        ('cache-ask', 'What did I decide about the cache directory?'),
        ('cache-answer', 'I decided the cache directory stays per version so a running session keeps the files it started with.'),
        ('notes', 'Notes: i think the Recall docs need work; Claude and Python mentions are everywhere in these notes, and the word i appears a lot, i i i.'),
    ]
    source = tmp_path / 'probe.jsonl'
    source.write_text(''.join(json.dumps({'type': 'assistant', 'uuid': key,
        'timestamp': '2026-09-08T00:00:00Z', 'message': {'role': 'assistant', 'content': text}}) + '\n'
        for key, text in messages))
    conn = get_connection(tmp_path / 'store.db')
    try:
        memory.index_file(conn, source, agent='claude', session_id='probe', cwd=str(tmp_path))
        conn.commit()
        ids = dict(conn.execute('SELECT id,message_key FROM memory_blocks'))
        capital = memory.search(conn, 'Why did the Recall hook time out?', limit=3, half_life=0)
        lower = memory.search(conn, 'why did the recall hook time out?', limit=3, half_life=0)
        assert [h['block_id'] for h in capital] == [h['block_id'] for h in lower]
        assert 'answer' in [ids[h['block_id']] for h in capital]
    finally:
        conn.close()


def test_overlapping_channel_passages_keep_both_explanations_with_exact_offsets():
    text = 'x' * 1200 + 'first reason' + 'y' * 388 + 'second reason' + 'z' * 900
    lexical = dict(hit('same', text[1440:]), end_char=len(text), start_char=1440)
    semantic = dict(hit('same', text[:1600]), end_char=1600)
    row = fuse([hit('noise'), lexical], [semantic], 5)[0]
    assert row['text'] == text
    assert row['start_char'] == 0 and row['end_char'] == len(text)
    assert row['get'] == 'get same --start 0'
    assert row['content_hash'] == 'retained-same'


def test_excerpt_merge_refuses_gap_different_revision_conflicting_overlap_and_oversize():
    from retrieval_ranking import merge_overlapping
    primary = dict(hit('same', 'abcd'), end_char=4)
    variants = [dict(hit('same', 'tail', 5), end_char=9),
                dict(hit('same', 'cdef', 2), end_char=6, content_hash='other'),
                dict(hit('same', 'XXef', 2), end_char=6),
                dict(hit('same', 'abcd' + 'x' * 3200), end_char=3204)]
    for other in variants:
        assert merge_overlapping(primary, other) == primary
