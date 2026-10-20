#!/usr/bin/env python3
"""Explicit, local-only, bounded operational diagnostics; never log history or queries."""
import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import stat
import sys

try:
    import fcntl
except ImportError:  # Optional diagnostics must not make retrieval platform-dependent.
    fcntl = None

MAX_BYTES = 1024 * 1024
HEADER = b'{"recall_diagnostics":1}\n'
OPERATIONS = {'recall_search', 'recall_get', 'recall_status', 'recall_brief', 'capture'}
OUTCOMES = {'ok', 'empty', 'invalid_request', 'query_budget', 'store_busy', 'store_unavailable', 'capture_errors'}
COUNTS = {'result_count', 'response_chars', 'blocks_updated', 'pending_files', 'passes', 'error_count'}


class Diagnostics:
    """Two 1 MiB JSONL segments, cooperative nonblocking lock, best-effort writes.

    No file is touched when path is absent. Only fixed enums and numeric fields
    are accepted, so exceptions, tool arguments and source identifiers cannot leak.
    Slow filesystem I/O is not a hard wall-clock bound. Unsupported locking or a
    full/unwritable/contended destination drops events instead of failing Recall.
    """
    def __init__(self, path=None):
        self.path = Path(path).expanduser().absolute() if path is not None else None
        self.dropped = 0
        self.warned = False

    def _drop(self):
        self.dropped += 1
        if not self.warned:
            print('Recall diagnostics: an event was dropped; check the local log destination or writer contention. Retrieval continues.', file=sys.stderr)
            self.warned = True

    @staticmethod
    def _open(path):
        flags = os.O_RDWR | os.O_CREAT | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0)
        fd = os.open(path, flags, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077:
                raise OSError('Diagnostics requires a private regular file')
            return fd
        except BaseException:
            os.close(fd)
            raise

    @staticmethod
    def _validate(fd):
        if os.fstat(fd).st_size > MAX_BYTES:
            raise OSError('Diagnostics file exceeds the configured bound')
        if os.fstat(fd).st_size and os.pread(fd, len(HEADER), 0) != HEADER:
            raise OSError('Not a Recall diagnostics file')

    def record(self, operation, outcome, elapsed_ms, **counts):
        if self.path is None:
            return
        if operation not in OPERATIONS or outcome not in OUTCOMES:
            return
        if type(elapsed_ms) not in (int, float) or not math.isfinite(elapsed_ms) or elapsed_ms < 0:
            return
        event = {'version': 1, 'at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                 'operation': operation, 'outcome': outcome, 'elapsed_ms': round(min(elapsed_ms, 86400000), 3)}
        event.update({k: min(v, 2**53-1) for k, v in counts.items() if k in COUNTS and type(v) is int and v >= 0})
        data = (json.dumps(event, separators=(',', ':'))+'\n').encode()
        lock = target = None
        try:
            if fcntl is None:
                raise OSError('File locking unavailable')
            lock = self._open(str(self.path)+'.lock')
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            target = self._open(self.path)
            self._validate(target)
            if os.fstat(target).st_size + len(data) > MAX_BYTES:
                previous = Path(str(self.path)+'.1')
                if previous.exists() or previous.is_symlink():
                    old = self._open(previous)
                    try:
                        self._validate(old)
                    finally:
                        os.close(old)
                os.close(target)
                target = None
                os.replace(self.path, previous)
                target = self._open(self.path)
            if os.fstat(target).st_size == 0:
                os.write(target, HEADER)
            os.lseek(target, 0, os.SEEK_END)
            # Small regular-file write; partial writes are treated as dropped and
            # the report ignores any incomplete line left by a crash/disk failure.
            if os.write(target, data) != len(data):
                raise OSError('Partial diagnostic write')
        except (OSError, ValueError):
            self._drop()
        finally:
            if target is not None:
                os.close(target)
            if lock is not None:
                os.close(lock)


def summarize(path):
    rows, malformed = [], 0
    for file in (Path(str(path)+'.1'), Path(path)):
        if not file.exists():
            continue
        with file.open('rb') as stream:
            if stream.readline(len(HEADER)+1) != HEADER:
                raise ValueError('Not a Recall diagnostics log')
            raw = stream.read(MAX_BYTES)
        for line in raw.splitlines():
            try:
                row = json.loads(line)
                if not isinstance(row, dict) or not isinstance(row.get('operation'), str) or not isinstance(row.get('outcome'), str) or row['operation'] not in OPERATIONS or row['outcome'] not in OUTCOMES:
                    raise ValueError('Invalid event')
                elapsed = row.get('elapsed_ms')
                if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < 0:
                    raise ValueError('Invalid duration')
                rows.append(row)
            except (ValueError, UnicodeDecodeError):
                malformed += 1
    operations = {}
    for operation in sorted(OPERATIONS):
        selected = [r for r in rows if r['operation'] == operation]
        if not selected:
            continue
        timings = sorted(r['elapsed_ms'] for r in selected)
        operations[operation] = {'calls': len(selected), 'outcomes': {k: sum(r['outcome'] == k for r in selected) for k in sorted({r['outcome'] for r in selected})},
            'p50_ms': timings[math.ceil(len(timings)*0.5)-1], 'p95_ms': timings[math.ceil(len(timings)*0.95)-1], 'max_ms': timings[-1],
            'response_chars': sum(r.get('response_chars', 0) for r in selected if type(r.get('response_chars', 0)) is int)}
    return {'events': len(rows), 'ignored_malformed_lines': malformed, 'operations': operations,
            'notice': 'Local retained sample only. Rotated, dropped, disabled and uninstrumented calls are absent. Character counts are not billed tokens or answer-quality scores.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path', type=Path, help='Explicit diagnostics JSONL path to summarize locally')
    args = parser.parse_args()
    try:
        print(json.dumps(summarize(args.path.expanduser()), indent=2))
    except (OSError, ValueError) as exc:
        parser.exit(1, str(exc)+'\n')


if __name__ == '__main__':
    main()
