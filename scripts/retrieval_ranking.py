"""Bounded, model-free ordering of retained passages.

These helpers rank candidates and combine overlapping source spans. They never
widen a reader's source scope, alter retained text, or treat a historical assertion
as a confirmed outcome.
"""
MAX_FUSED_EXCERPT_CHARS = 3200


def merge_overlapping(primary, alternate):
    """Keep both channel matches when they form one bounded verified source span."""
    if (primary.get('block_id') != alternate.get('block_id') or
            not primary.get('content_hash') or
            primary['content_hash'] != alternate.get('content_hash')):
        return primary
    spans = sorted((primary, alternate), key=lambda r: r['start_char'])
    left, right = spans
    start = left['start_char']; end = max(left['end_char'], right['end_char'])
    if (end - start > MAX_FUSED_EXCERPT_CHARS or right['start_char'] > left['end_char'] or
            any(r['end_char'] - r['start_char'] != len(r['text']) for r in spans)):
        return primary
    overlap_end = min(left['end_char'], right['end_char'])
    if left['text'][right['start_char']-start:overlap_end-start] != right['text'][:overlap_end-right['start_char']]:
        return primary
    text = left['text'] + right['text'][max(0, left['end_char']-right['start_char']):]
    return dict(primary, text=text, start_char=start, end_char=end,
                get=f"get {primary['block_id']} --start {start}")


def fuse(lexical, semantic, limit):
    """Best-rank fusion; agreement breaks ties rather than overwhelming a lead.

    Reciprocal-rank addition with a large damping constant can put many weak
    overlapping matches ahead of either channel's best result. Here each
    channel's first result remains in the first two distinct blocks. Repeated
    chunks of one block cannot earn extra votes or displace another block.
    The returned excerpt is the passage from the channel that supplied its best
    rank. If both channel matches overlap in the same retained revision, their
    union is preserved up to 3,200 characters so neither explanation is lost.
    """
    candidates = {}
    for channel, hits in enumerate((lexical, semantic)):
        seen = set()
        for row in hits:
            key = row['block_id']
            if key in seen:
                continue
            rank = len(seen)
            seen.add(key)
            item = candidates.setdefault(key, {'ranks': {}, 'rows': {}})
            item['ranks'][channel] = rank
            item['rows'][channel] = row
    def order(pair):
        key, item = pair
        ranks = item['ranks']
        best = min(ranks.values())
        other = max(ranks.values()) if len(ranks) == 2 else float('inf')
        # Prefer corroboration only at the same best rank. Lexical wins a
        # remaining channel tie, preserving literal identifiers/commands.
        return (best, other, ranks.get(0, float('inf')), key)
    result = []
    for _, item in sorted(candidates.items(), key=order)[:limit]:
        channel = min(item['ranks'], key=lambda c: (item['ranks'][c], c))
        row = item['rows'][channel]
        if len(item['rows']) == 2 and all('end_char' in r for r in item['rows'].values()):
            row = merge_overlapping(row, item['rows'][1-channel])
        result.append(dict(row, fusion_score=1.0 / (1 + item['ranks'][channel])))
    return result
