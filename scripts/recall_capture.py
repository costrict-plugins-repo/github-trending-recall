#!/usr/bin/env python3
"""Refresh explicitly selected Claude/Codex histories without requiring Claude hooks.

One invocation makes fair incremental progress within a soft time budget. --watch
keeps refreshing in the foreground; Ctrl-C stops it. This does not install a daemon.
"""
import argparse
from collections import deque
import json
import math
from pathlib import Path
import sqlite3
import time

from db import get_connection
import memory_store as memory
from recall_diagnostics import Diagnostics


class CaptureWorker:
    def __init__(self, conn, roots, agent, recursive=False, cwd=None):
        self.conn, self.roots, self.agent = conn, [Path(p).expanduser().resolve() for p in roots], agent
        self.recursive = recursive
        self.cwd = str(Path(cwd).expanduser().resolve()) if cwd else ''
        self.pending = deque()
        self.signatures = {}

    def discover(self):
        files, explicit_files, errors = set(), set(), []
        for root in self.roots:
            try:
                if root.is_file():
                    files.add(root)
                    explicit_files.add(root)
                elif root.is_dir():
                    # Do not follow symlink files out of an explicitly selected source root.
                    files.update(memory.transcript_paths(root, self.agent, self.recursive))
                else:
                    errors.append({'path': str(root), 'state': 'source_missing'})
            except OSError:
                errors.append({'path': str(root), 'state': 'unreadable'})
        changed = []
        for path in files:
            try:
                st = path.stat()
                signature = (st.st_mtime_ns, st.st_size)
                if self.signatures.get(path) != signature:
                    if path not in explicit_files and not memory.looks_like_transcript(path, agent=self.agent):
                        # Reconsider a skipped sidecar only if it changes, so a
                        # partial writer can later become a recognizable source.
                        self.signatures[path] = signature
                        continue
                    changed.append((path, signature))
            except OSError:
                errors.append({'path': str(path), 'state': 'source_missing'})
        # Keep unfinished sources at the front across cycles; newer arrivals cannot starve them.
        existing = set(self.pending)
        self.pending.extend(path for path, _ in sorted(changed, key=lambda p: str(p[0])) if path not in existing)
        return dict(changed), errors

    def refresh(self, seconds=4):
        start = time.monotonic()
        signatures, errors = self.discover()
        deadline = start + seconds
        states, passes, updated = {}, 0, 0
        while self.pending and time.monotonic() < deadline:
            path = self.pending.popleft()
            try:
                result = memory.index_mapped_file(self.conn, path, agent=self.agent, cwd=self.cwd)
                if result['state'] == 'scope_mismatch':
                    self.conn.rollback()
                    errors.append({'path': str(path), 'state': result['state'],
                                   'next_action': result['next_action']})
                    continue
                self.conn.commit()
            except (OSError, ValueError, sqlite3.Error) as exc:
                self.conn.rollback()
                errors.append({'path': str(path), 'state': 'error', 'error': type(exc).__name__})
                continue
            passes += 1
            updated += result.get('blocks', 0)
            states[str(path)] = result
            if result['state'] in ('backlog', 'rebuilding'):
                self.pending.append(path)
            elif result['state'] == 'complete' and path in signatures:
                # A concurrent append after discovery will differ on the next cycle.
                self.signatures[path] = signatures[path]
            # partial_record/source_changed need a later retry or explicit rebuild, not a busy loop.
        return {'agent': self.agent, 'passes': passes, 'blocks_updated': updated, 'states': states,
                'pending_files': len(self.pending), 'errors': errors, 'seconds': round(time.monotonic()-start, 3),
                'budget': 'Soft wall-clock budget includes discovery; a directory scan or one record can exceed it.',
                'note': 'Original transcripts are read only. Source edits requiring rebuild are never rebuilt automatically.'}


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError('must be a finite positive number')
    return number


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db', type=Path, required=True, help='Explicit local store to create/migrate/update')
    p.add_argument('--agent', choices=['claude', 'codex'], required=True)
    p.add_argument('--path', type=Path, action='append', required=True, help='Authorized JSONL file or source directory; repeat for additional roots')
    p.add_argument('--seconds', type=positive, default=4, help='Soft budget per cycle; default 4 seconds')
    p.add_argument('--watch', action='store_true', help='Continue in the foreground until interrupted')
    p.add_argument('--recursive', action='store_true', help='Include Claude subdirectories; Codex date directories are always searched')
    p.add_argument('--cwd', type=Path, help='Explicit host repository mapping for selected histories (e.g. Cowork VM paths); existing foreign scopes require explicit rescope')
    p.add_argument('--interval', type=positive, default=10, help='Seconds between cycles in watch mode; default 10')
    p.add_argument('--diagnostics', type=Path, help='Opt-in bounded local metrics log; no source paths or transcript text')
    args = p.parse_args()
    for root in args.path:
        if not root.expanduser().exists():
            p.error('Source path does not exist: ' + str(root))
    conn = get_connection(args.db.expanduser())
    worker = CaptureWorker(conn, args.path, args.agent, recursive=args.recursive, cwd=args.cwd)
    diagnostics = Diagnostics(args.diagnostics)
    try:
        while True:
            result = worker.refresh(args.seconds)
            diagnostics.record('capture', 'capture_errors' if result['errors'] else 'ok', result['seconds']*1000,
                passes=result['passes'], blocks_updated=result['blocks_updated'],
                pending_files=result['pending_files'], error_count=len(result['errors']))
            print(json.dumps(result), flush=True)
            if not args.watch:
                # Incomplete progress is visible in JSON; missing/unreadable inputs fail the invocation.
                return 1 if result['errors'] else 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    finally:
        conn.close()


if __name__ == '__main__':
    raise SystemExit(main())
