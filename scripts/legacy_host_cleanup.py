"""Explicit cleanup of proven host prompts without renumbering legacy exchanges."""
import json
from pathlib import Path

from utils import extract_text_content, redact_secrets, truncate_text, MAX_CHARS_PER_MESSAGE
from db import _insert_fts_rows


def clean(conn, session_id, apply=False):
    session = conn.execute('SELECT transcript_path FROM sessions WHERE session_id=?', (session_id,)).fetchone()
    if session is None or not session['transcript_path']:
        raise ValueError('A registered legacy session with its original transcript is required')
    path = Path(session['transcript_path'])
    host, ordinary = set(), set()
    with path.open('rb') as stream:
        for raw in stream:
            if not raw.endswith(b'\n'):
                break
            try:
                row = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(row, dict) or row.get('type') != 'user':
                continue
            message = row.get('message')
            if not isinstance(message, dict):
                continue
            text = truncate_text(redact_secrets(extract_text_content(message)), MAX_CHARS_PER_MESSAGE)
            key = (row.get('timestamp', ''), text)
            (host if row.get('isMeta') or row.get('isCompactSummary') else ordinary).add(key)
    proven = host - ordinary
    # Parse the original outside the write transaction. Lock before reading the
    # indexed rows so an arriving assistant continuation cannot make the FTS
    # delete payload stale between selection and update. The caller commits.
    if apply and not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    matches = [dict(r) for r in conn.execute('SELECT * FROM exchanges WHERE session_id=?', (session_id,))
               if (r['timestamp'], r['user_text']) in proven]
    if apply:
        for row in matches:
            conn.execute('''INSERT INTO exchanges_fts(exchanges_fts,rowid,user_text,assistant_text,preview,tool_text)
                VALUES('delete',?,?,?,?,?)''', (row['id'],row['user_text'],row['assistant_text'],row['preview'],row['tool_text']))
            conn.execute("UPDATE exchanges SET user_text='',preview='[Host context omitted]' WHERE id=?", (row['id'],))
            _insert_fts_rows(conn, [row['id']])
    return {'session_id':session_id, 'matched':len(matches), 'applied':apply,
            'ambiguous_keys_skipped':len(host & ordinary),
            'note':'Exact timestamp and redacted capped text matched to flagged original records. IDs, indices, replies, tools and annotations preserved; original transcripts unchanged.'}
