"""Astra's P54 follow-up (verification of 82988a6): indented and mixed host wrappers must
not re-enter compaction excerpts. Authored by Astra; only the repo root is made relative."""
import json
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'scripts'), str(ROOT/'hooks')]
import memory_store as memory
from db import get_connection
from post_compact import build_recovery_context


@pytest.mark.parametrize('body', [
    '  \n<system-reminder>HOST_SENTINEL</system-reminder>',
    'Real prefix <system-reminder>HOST_SENTINEL</system-reminder> real tail',
    '<system-reminder>first wrapper</system-reminder>Real prefix '
    '<system-reminder>HOST_SENTINEL</system-reminder> real tail',
])
def test_recovery_never_reintroduces_host_metadata(tmp_path, body):
    conn = get_connection(tmp_path/'store.db')
    try:
        path = tmp_path/'trace.jsonl'
        rows = [{'type': role, 'uuid': str(i), 'sessionId': 'mixed',
                 'timestamp': '2026-09-06', 'message': {'role': role, 'content': text}}
                for i, (role,text) in enumerate([('user','Actual opening request'),('assistant',body)])]
        path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        memory.index_file(conn,path,session_id='mixed',cwd=str(tmp_path))
        conn.commit()
        out = build_recovery_context(conn,'mixed')
        assert 'HOST_SENTINEL' not in out
        assert 'Actual opening request' in out
        if body.endswith('real tail'):
            assert f"--start {body.rfind('real tail')}" in out
        # Evidence itself is retained; only injected excerpts are filtered.
        assert conn.execute("SELECT text FROM memory_blocks WHERE role='assistant'").fetchone()[0] == body
    finally:
        conn.close()
