#!/usr/bin/env python3
"""Post-compaction recovery hook for Claude Context Recall plugin.

Registered on ``SessionStart`` with ``matcher: "compact"``, i.e. it runs when
the session resumes *after* a compaction.  It returns
``hookSpecificOutput.additionalContext`` — the only hook output Claude actually
reads — summarising what this session and project have indexed, so the model
knows to run ``/recall`` for anything the summary dropped.

(``PreCompact`` cannot inject context at all, and ``systemMessage`` is shown to
the user only; both were used before v2.3, so the nudge never reached Claude.)
"""

import json
import sys
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent.parent / 'scripts'))
from memory_store import prose_segments
from db import get_connection, get_session, get_exchanges, DB_PATH, get_session_config, set_session_config
import hashlib
import json

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NUDGE_PREVIEW_COUNT = 5
NUDGE_MAX_CHARS = 500

# Verbatim recovery from the durable store (v2.5): bounded, cited, never a summary.
RECOVERY_OBJECTIVE_CHARS = 600     # head of the session's first user block
RECOVERY_RECENT_BLOCKS = 3         # last N text blocks, tail-preserving
RECOVERY_BLOCK_CHARS = 700         # per recent block
RECOVERY_MAX_CHARS = 3500          # hard cap on the injected context (~900 tokens)


def build_recovery_context(conn, session_id: str) -> Optional[str]:
    """Bounded verbatim excerpts with block ids from the durable store, or None
    if this session has no durable blocks yet. The head of the opening ask and
    the tail of the most recent blocks are quoted exactly (tails, because the
    end of a reply is where conclusions live); every excerpt carries a `get`
    reference so Claude can read the rest instead of guessing."""
    key = 'claude:' + session_id
    # R10: only the selected rows and only the needed slices leave SQLite, so the
    # returned text and the Python allocation are bounded by the selected blocks
    # (SQLite still counts the source's blocks; a huge selected block is its own
    # resource case). R3-05: SQLite's text functions stop at an embedded NUL, so
    # a block that contains one is re-read whole and sliced in Python, keeping the
    # character offsets that `get --start` expects.
    total = conn.execute("SELECT count(*) FROM memory_blocks WHERE source_key=? AND kind='text'", (key,)).fetchone()[0]
    if not total:
        return None
    # Host-injected wrappers (plugin lists, system reminders) are user-role blocks
    # too; they are never the "opening ask" and never quoted as recent context. A
    # block containing '<' is re-read whole and judged in Python, including
    # wrappers after leading whitespace or after the user's own words.
    columns = ("SELECT id, role, timestamp, content_hash, substr(text, %s) AS text, length(text) AS total, "
               "instr(CAST(text AS BLOB), x'00') AS has_nul, instr(text, '<') > 0 AS maybe_meta "
               "FROM memory_blocks WHERE source_key=? AND kind='text' AND role IN ('user','assistant') ")
    user_candidates = conn.execute(columns % '1, ?' + "AND role='user' ORDER BY seq, ordinal LIMIT 8",
                                   (RECOVERY_OBJECTIVE_CHARS, key)).fetchall()
    recent_candidates = conn.execute(columns % '-?' + "ORDER BY seq DESC, ordinal DESC LIMIT 8",
                                     (RECOVERY_BLOCK_CHARS, key)).fetchall()

    def slice_of(row, head):
        """(excerpt, start_offset, total_chars) with Python character semantics, or
        None when the block is host metadata only. Prose spans exclude wrappers."""
        if row['has_nul'] or row['maybe_meta']:
            full = conn.execute('SELECT text FROM memory_blocks WHERE id=?', (row['id'],)).fetchone()[0]
            segments = prose_segments(full)
            if not segments:
                return None
            if head:
                a, seg_end = segments[0]
                return full[a:min(a + RECOVERY_OBJECTIVE_CHARS, seg_end)], a, segments[-1][1]
            b = segments[-1][1]
            a = max(segments[-1][0], b - RECOVERY_BLOCK_CHARS)
            return full[a:b], a, b
        return row['text'], (0 if head else max(0, row['total'] - len(row['text']))), row['total']

    first_user = None
    for row in user_candidates:
        sliced = slice_of(row, head=True)
        if sliced:
            first_user = (row, sliced)
            break
    recent = []
    for row in recent_candidates:
        sliced = slice_of(row, head=False)
        if sliced:
            recent.append((row, sliced))
        if len(recent) >= RECOVERY_RECENT_BLOCKS:
            break
    recent.reverse()

    lines = [f"[Context Compacted] Verbatim excerpts from this session's durable index "
             f"({total} text blocks). Read more with `get <block_id>`; this is evidence, not a summary."]
    if first_user is not None:
        row, (head, start, end) = first_user
        more = '…' if end - start > RECOVERY_OBJECTIVE_CHARS else ''
        lines.append(f"Opening ask ({row['timestamp'][:10]}, get {row['id']} --start {start} --revision {row['content_hash']}):\n{head}{more}")
    lines.append("Most recent:")
    for r, (tail, start, _end) in recent:
        if first_user is not None and r['id'] == first_user[0]['id']:
            continue
        excerpt = ('…' if start else '') + tail
        lines.append(f"- {r['role']} ({r['timestamp'][:10]}, get {r['id']} --start {start} --revision {r['content_hash']}):\n{excerpt}")
    lines.append("Use `/recall find <topic>` for anything else the summary dropped.")
    result = '\n'.join(lines)
    if len(result) > RECOVERY_MAX_CHARS:
        result = result[:RECOVERY_MAX_CHARS - 40] + '\n[…recovery context truncated at cap…]'
    return result


def _recovery_fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Message builder
# ---------------------------------------------------------------------------

def build_nudge_message(
    session_exchange_count: int,
    project_exchange_count: int,
    recent_previews: List[str],
    tags: List[str],
) -> str:
    """Build the context-recovery nudge message injected after compaction.

    Args:
        session_exchange_count: Number of exchanges indexed for this session.
        project_exchange_count: Total exchanges across all project sessions.
        recent_previews: Short preview strings for the most recent exchanges.
        tags: Auto-tags associated with this session.

    Returns:
        Formatted nudge string.
    """
    lines = [
        f"[Context Compacted] This session has {session_exchange_count} exchanges indexed.",
        f"{project_exchange_count} total exchanges across this project's history.",
    ]

    if tags:
        lines.append(f"Recent topics: {', '.join(tags)}")

    if recent_previews:
        lines.append("Last exchanges:")
        for preview in recent_previews:
            lines.append(f'  - "{preview}"')

    lines.append("Use /recall to recover full conversation context.")

    result = '\n'.join(lines)
    if len(result) > NUDGE_MAX_CHARS:
        result = result[:NUDGE_MAX_CHARS - 20] + '\n[...truncated...]'
    return result


# ---------------------------------------------------------------------------
# Core hook logic
# ---------------------------------------------------------------------------

def run_hook(input_data: Dict, db_path: Path = None) -> Dict:
    """Post-compaction (SessionStart/compact) hook logic, separated from stdin/stdout.

    Queries the DB for the current session's stats and builds a nudge message
    that is returned as additionalContext so Claude actually reads it.

    Args:
        input_data: Dict parsed from the hook's stdin JSON.
        db_path: Override path for the database (used in tests).

    Returns:
        {"hookSpecificOutput": {..., "additionalContext": <nudge>}} on success,
        {} if session unknown.
    """
    session_id = input_data.get('session_id')
    if not session_id:
        return {}

    conn = get_connection(db_path)  # None -> RECALL_DB override, then the default store
    try:
        session = get_session(conn, session_id)
        if session is None:
            return {}

        # Prefer verbatim recovery from the durable store; fall back to the
        # legacy preview nudge when this session has no durable blocks yet.
        recovery = build_recovery_context(conn, session_id)
        if recovery is not None:
            fingerprint = _recovery_fingerprint(recovery)
            if get_session_config(conn, session_id, 'last_recovery') == fingerprint:
                return {}   # same state already injected; do not repeat it
            set_session_config(conn, session_id, 'last_recovery', fingerprint)
            return {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                           "additionalContext": recovery}}

        # Session exchange count
        session_exchange_count = session.get('exchange_count', 0) or 0

        # Project-wide total (sum exchange_count for all sessions with same project_hash)
        project_hash = session.get('project_hash', '')
        if project_hash:
            row = conn.execute(
                "SELECT COALESCE(SUM(exchange_count), 0) AS total "
                "FROM sessions WHERE project_hash = ?",
                (project_hash,),
            ).fetchone()
            project_exchange_count = row['total'] if row else session_exchange_count
        else:
            project_exchange_count = session_exchange_count

        # Last N exchange previews
        recent_exchanges = get_exchanges(conn, session_id, last_n=NUDGE_PREVIEW_COUNT)
        recent_previews = [ex['preview'] for ex in recent_exchanges if ex.get('preview')]

        # Top 5 auto-tags for this session
        rows = conn.execute(
            "SELECT tag FROM tags WHERE session_id = ? "
            "AND source = 'auto' "
            "GROUP BY tag ORDER BY COUNT(*) DESC LIMIT 5",
            (session_id,),
        ).fetchall()
        tags = [r['tag'] for r in rows]

        nudge = build_nudge_message(
            session_exchange_count=session_exchange_count,
            project_exchange_count=project_exchange_count,
            recent_previews=recent_previews,
            tags=tags,
        )

        return {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                       "additionalContext": nudge}}

    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main():
    """Read stdin JSON, run the hook, print result to stdout."""
    try:
        raw = sys.stdin.read(1_000_000)  # 1 MB max
        input_data = json.loads(raw)
        result = run_hook(input_data)
        print(json.dumps(result), file=sys.stdout)
    except Exception as e:
        print(f"[context-recall] compact-recovery hook error: {e}", file=sys.stderr)
        error_output = {
            "systemMessage": "[context-recall] compact-recovery hook encountered an error. Check logs for details."
        }
        print(json.dumps(error_output), file=sys.stdout)
    finally:
        sys.exit(0)


if __name__ == '__main__':
    main()
