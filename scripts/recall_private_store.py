"""Private-store backend for the dedicated native controller and owner maintenance.

The caller must be a trusted controller with an exclusive owner-bound connection.
Project-wide app connectors do not meet that contract. Plain CLI/MCP readers are
refused; same-OS-user raw file access is outside this access-control boundary.
"""
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from db import get_connection, get_read_connection
import memory_store as memory
import recall_privacy as policy
from recall_mcp import RecallService, read_connection


class SessionStore:
    def __init__(self, path, owner, repo_id, shared_path=None):
        self.path, self.owner, self.repo_id = Path(path), policy.validate_key(owner), repo_id
        self.shared_path = Path(shared_path) if shared_path else None

    @classmethod
    def create(cls, shared, directory, owner, cwd):
        policy.validate_key(owner)
        if owner.split(':',1)[0] not in ('claude','codex') or '/' in owner.split(':',1)[1]:
            raise ValueError('Only an explicitly bound root coding session is supported')
        if not shared.in_transaction: shared.execute('BEGIN IMMEDIATE')
        if shared.execute('SELECT 1 FROM memory_sources WHERE source_key=?',(owner,)).fetchone() or (
            owner.startswith('claude:') and shared.execute('SELECT 1 FROM sessions WHERE session_id=?',(owner.split(':',1)[1],)).fetchone()):
            raise ValueError('Existing shared history requires a reviewed conversion; it cannot be silently made private')
        shared_path = shared.execute('PRAGMA database_list').fetchone()[2]
        if not shared_path:
            raise ValueError('Private routing requires a persistent shared policy store')
        policy.set_mode(shared,owner,'off')
        result = cls._create_file(shared_path, directory, owner, cwd)
        with policy.journal_lock(shared):
            shared.execute('BEGIN IMMEDIATE')
            policy.write_journal(shared, policy.journal_state(shared), private={owner})
            shared.commit()
        return result

    @classmethod
    def _create_file(cls, shared_path, directory, owner, cwd):
        """Trusted transition primitive: creates an empty, tagged owner store."""
        directory=Path(directory).expanduser().resolve()
        directory.mkdir(mode=0o700,parents=True,exist_ok=True)
        path=directory/(memory.digest(owner)+'.db')
        if path.exists(): raise ValueError('Private destination already exists')
        fd, name = tempfile.mkstemp(prefix='.recall-private-', suffix='.db', dir=directory)
        os.close(fd)
        staging = Path(name)
        repo_id=memory.repository_identity(cwd)
        private = None
        try:
            private=get_connection(staging)
            private.execute('PRAGMA synchronous=FULL')
            private.execute('PRAGMA application_id='+str(policy.PRIVATE_APPLICATION_ID))
            private.execute('CREATE TABLE recall_private_owner (owner TEXT PRIMARY KEY,repo_id TEXT NOT NULL)')
            private.execute('INSERT INTO recall_private_owner VALUES(?,?)',(owner,repo_id))
            for sql in policy.PRIVATE_GUARDS.values():
                private.execute(sql)
            private.commit()
            private.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            # Publish a standalone file. Apple's SQLite cannot open a copied
            # WAL-mode file read-only before a writer creates its sidecars.
            # Capture selects WAL again on its first authorized writer open.
            private.execute('PRAGMA journal_mode=DELETE')
            private.close(); private = None
            # Publish only the complete, empty, tagged store; link refuses to
            # replace a destination created concurrently. No content is in it yet.
            os.link(staging, path)
            fd=os.open(directory,os.O_RDONLY)
            try: os.fsync(fd)
            finally: os.close(fd)
        finally:
            if private is not None: private.close()
            for suffix in ('', '-wal', '-shm', '.access.lock'):
                owned = Path(str(staging) + suffix)
                if owned.exists(): owned.unlink()
        # Only a writer creates the final access lease; future readers create nothing.
        from recall_access import Lease
        with Lease(path, create=True): pass
        from recall_private_control import initialize
        initialize(path, owner)
        return cls(path,owner,repo_id,shared_path)

    def capture(self, transcript, *, publish_rebuild=False, rebuild=False):
        agent,sid=self.owner.split(':',1)
        actual=policy.native_identity(transcript,agent)
        if actual!=sid or Path(transcript).parent.name=='subagents':
            raise ValueError('Private source identity mismatch; no inherited subagent or fork grant')
        if self.shared_path is None:
            raise ValueError('Shared suppression binding is missing; private capture refused')
        shared=get_connection(self.shared_path)
        # Serialize with owner mode changes until the private capture commits.
        shared.execute('BEGIN IMMEDIATE')
        c=None
        try:
            if policy.mode(shared,self.owner) != 'off':
                raise ValueError('Shared suppression is no longer active; private capture refused')
            c=get_connection(self.path,private_owner=self.owner)
            from recall_private_control import read
            control = read(self.path, self.owner)
            if control['access'] != 'active' or control['capture'] != 'on':
                return {'state': 'capture_disabled', 'source_key': self.owner}
            stored=c.execute('SELECT repo_id FROM recall_private_owner').fetchone()[0]
            if stored!=self.repo_id:raise ValueError('Private repository binding mismatch')
            r=memory.index_file(c,transcript,agent=agent,session_id=sid,publish_rebuild=publish_rebuild,rebuild=rebuild)
            c.execute('UPDATE memory_sources SET repo_id=?,scope_pinned=1 WHERE source_key=?',(stored,self.owner))
            c.commit();return r
        finally:
            if c is not None:c.close()
            shared.rollback();shared.close()

    def reader(self):
        from recall_private_control import read
        return SessionRecallService(self.path,self.repo_id,self.owner,read(self.path,self.owner)['epoch'])


class SessionRecallService(RecallService):
    """Construction is a trusted host operation; tool arguments cannot select owner."""
    def __init__(self,path,repo_id,owner,epoch=None):
        super().__init__(path,repo_id)
        self.owner=policy.validate_key(owner)
        self.epoch=epoch

    @contextmanager
    def open_connection(self):
        with read_connection(self.db_path,private_owner=self.owner) as conn:
            from recall_private_control import read, authorize
            if self.epoch is None:
                self.epoch = read(self.db_path, self.owner)['epoch']
            authorize(self.db_path, self.owner, self.epoch)
            row=conn.execute('SELECT repo_id FROM recall_private_owner').fetchone()
            if not row or row[0]!=self.repo_id:
                raise ValueError('Private repository binding mismatch')
            yield conn

    def coverage(self,conn,*args,**kwargs):
        bound=conn.execute('SELECT owner,repo_id FROM recall_private_owner').fetchone()
        if not bound or bound['owner']!=self.owner or bound['repo_id']!=self.repo_id:
            raise ValueError('Private reader binding mismatch')
        result=super().coverage(conn,*args,**kwargs)
        if not kwargs.get('compact'):
            result['privacy']={'reader_binding':'exclusive owner connection required',
                               'host_filesystem_isolation':'unverified',
                               'native_host_activation':'dedicated root controller only; shared app connectors are not session-bound'}
        return result
