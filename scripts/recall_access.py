"""Cooperative connection leases for offline privacy maintenance.

These synchronize updated Recall clients, not raw same-user SQLite/file access.
No network, daemon or model-controlled maintenance bypass.
"""
try:
    import fcntl
except ImportError:  # Ordinary shared use remains available without POSIX locks.
    fcntl = None
import os
import sqlite3
from pathlib import Path

from recall_privacy import _safe_file


class StoreAccessError(ValueError):
    def __init__(self, message, outcome='store_unavailable'):
        super().__init__(message)
        self.outcome = outcome


def canonical(path):
    return Path(path).expanduser().resolve()


def gate_path(path):
    return Path(str(canonical(path)) + '.privacy-transition.json')


class Lease:
    def __init__(self, path, *, create=False, exclusive=False, recovery=False):
        self.fd = None
        if fcntl is None:
            raise ValueError('Privacy maintenance requires POSIX connection locks on this candidate host')
        self.path = canonical(path)
        self.exclusive, self.fd = exclusive, None
        flags = (os.O_RDWR | os.O_CREAT if create else os.O_RDONLY) | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            fd = os.open(str(self.path) + '.access.lock', flags, 0o600)
        except FileNotFoundError as exc:
            raise StoreAccessError('Store access lease is missing; run the matching writer/doctor setup before reading this copied store') from exc
        try:
            _safe_file(fd, 'Store access lease')
            try:
                fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise StoreAccessError('Store has active connections or privacy maintenance; retry after they close', 'store_busy') from exc
            if os.path.lexists(gate_path(self.path)) and not (exclusive and recovery):
                raise StoreAccessError('Privacy transition is incomplete; owner recovery is required before store access')
            self.fd = fd
        except BaseException:
            os.close(fd)
            raise

    def close(self):
        if getattr(self, 'fd', None) is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def __del__(self):
        self.close()

    def authorizes(self, path):
        return self.fd is not None and self.exclusive and self.path == canonical(path)


class Connection(sqlite3.Connection):
    lease = None

    def close(self):
        try:
            super().close()
        finally:
            if self.lease is not None:
                self.lease.close()
                self.lease = None

    def __del__(self):
        try:
            self.close()
        except sqlite3.Error:
            pass


def connect(path, *, read_only=False, maintenance_lease=None):
    if str(path) == ':memory:':
        return sqlite3.connect(':memory:', factory=Connection)
    path = canonical(path)
    if fcntl is None:
        if maintenance_lease is not None or os.path.lexists(gate_path(path)):
            raise ValueError('Privacy maintenance is unavailable on this host; access refused')
        return sqlite3.connect(path.as_uri() + '?mode=ro' if read_only else str(path),
                               uri=read_only, timeout=0.5 if read_only else 5, factory=Connection)
    if maintenance_lease is not None and not maintenance_lease.authorizes(path):
        raise ValueError('Exclusive maintenance lease does not authorize this store')
    lease = None if maintenance_lease else Lease(path, create=not read_only)
    try:
        conn = sqlite3.connect(path.as_uri() + '?mode=ro' if read_only else str(path),
                               uri=read_only, timeout=0.5 if read_only else 5,
                               factory=Connection)
        conn.lease = lease
        return conn
    except BaseException:
        if lease:
            lease.close()
        raise
