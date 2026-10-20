#!/usr/bin/env python3
"""Opt-in, bounded capture of exactly the Codex hook's selected root transcript.

Hook metadata is routing information, not proof of a protected privacy principal.
No directory sweep, tool output injection, daemon, or hook trust manipulation.
"""
import argparse
import json
import math
from pathlib import Path
import sys
import time

import memory_store as memory
from db import get_connection

EVENTS = ('SessionStart', 'UserPromptSubmit', 'Stop', 'SessionEnd', 'Interrupt')


def capture(event, db, root, cwd=None, seconds=1.5):
    if not isinstance(event, dict) or event.get('hook_event_name') not in EVENTS:
        raise ValueError('Unsupported root capture event')
    sid, raw = event.get('session_id'), event.get('transcript_path')
    if not isinstance(sid, str) or not sid or not isinstance(raw, str) or not raw:
        raise ValueError('Hook requires a session identity and transcript path')
    if not math.isfinite(seconds) or not 0 < seconds <= 2:
        raise ValueError('Capture budget must be in (0, 2] seconds')
    root = Path(root).expanduser().resolve(strict=True)
    offered = Path(raw).expanduser()
    if offered.is_symlink():
        raise ValueError('Symlinked transcript refused')
    path = offered.resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError('Transcript is outside the selected capture root')
    # Require actual session metadata, not the filename fallback used by manual imports.
    identity = None
    with path.open('rb') as stream:
        for _ in range(30):
            line = stream.readline(256 * 1024 + 1)
            if len(line) > 256 * 1024:
                raise ValueError('Transcript metadata exceeds the hook discovery budget')
            if not line: break
            try: record = json.loads(line)
            except (ValueError, UnicodeDecodeError): continue
            if isinstance(record,dict) and record.get('type') == 'session_meta':
                payload = record.get('payload')
                if isinstance(payload,dict): identity = payload.get('id') or payload.get('session_id')
                break
    if identity != sid:
        raise ValueError('Hook/transcript identities disagree; subagents are not implicitly captured as their parent')
    start = time.monotonic(); result = None; passes = 0
    conn = get_connection(db)
    try:
        if cwd:
            target = memory.repository_identity(cwd)
            existing = conn.execute('SELECT repo_id,scope_pinned FROM memory_sources WHERE source_key=?',('codex:'+sid,)).fetchone()
            if not (existing and existing['scope_pinned'] and existing['repo_id'] == target):
                event_cwd = event.get('cwd')
                if not isinstance(event_cwd,str) or not event_cwd or memory.repository_identity(event_cwd) != target:
                    raise ValueError('Ambiguous hook repository mapping; register and explicitly pin this selected source first')

        while time.monotonic() - start < seconds:
            result = memory.index_mapped_file(conn, path, agent='codex', cwd=cwd or '', publish_rebuild=False)
            conn.commit(); passes += 1
            if result['state'] not in ('backlog','rebuilding'): break
        return {'passes': passes, 'result': result, 'seconds': round(time.monotonic()-start, 4),
                'note':'Soft bounded capture; staged rebuilds require explicit publication. No private owner binding is established by this hook.'}
    finally:
        conn.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db',type=Path,required=True)
    p.add_argument('--root',type=Path,required=True)
    p.add_argument('--cwd',type=Path)
    args=p.parse_args()
    try:
        raw=sys.stdin.read(65537)
        if len(raw)>65536: raise ValueError('Hook input exceeds 64 KiB')
        result=capture(json.loads(raw),args.db,args.root,args.cwd)
        state=(result.get('result') or {}).get('state')
        if state not in ('complete','capture_disabled'):
            print('[Recall capture] '+str(state or 'budget exhausted')+'; check scoped coverage.',file=sys.stderr)
    except (OSError,ValueError,RuntimeError) as exc:
        print('[Recall capture] '+str(exc),file=sys.stderr)
    except Exception as exc:
        print('[Recall capture] '+type(exc).__name__+'; capture skipped.',file=sys.stderr)
    # Advisory: no transcript text, routing metadata or errors injected into model context.
    return 0


if __name__=='__main__':
    raise SystemExit(main())
