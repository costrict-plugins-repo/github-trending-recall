"""Owner controls: disclosure scope, interrupted deletion, revocation and capture."""
import json
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
from db import get_connection
from recall_private_store import SessionStore
from recall_mcp import RecallService
import recall_private_control as control
import recall_privacy as policy


def write(path, text, append=False):
    with path.open('a' if append else 'w') as f:
        if not append:
            f.write(json.dumps({'type':'session_meta','payload':{'id':'owner','cwd':str(path.parent)}})+'\n')
        f.write(json.dumps({'type':'response_item','payload':{'type':'message','id':str(path.stat().st_size),'role':'user','content':[{'type':'input_text','text':text}]}})+'\n')


@pytest.fixture
def private(tmp_path):
    c = get_connection(tmp_path/'shared.db')
    s = SessionStore.create(c, tmp_path/'private', 'codex:owner', tmp_path)
    c.close()
    trace = tmp_path/'owner.jsonl'
    write(trace, 'Publish this. Keep this secret.')
    s.capture(trace)
    return s, trace


def test_pause_retains_reads_and_resume_requires_backfill_consent(private):
    s, trace = private
    control.change(s, 'pause')
    write(trace, 'Disabled interval marker', True)
    assert s.capture(trace)['state'] == 'capture_disabled'
    assert s.reader().call('recall_search', {'query':'Publish'})['hits']
    assert not s.reader().call('recall_search', {'query':'Disabled'})['hits']
    with pytest.raises(ValueError, match='backfill'): control.change(s, 'resume')
    control.change(s, 'resume', allow_backfill=True)
    s.capture(trace)
    assert s.reader().call('recall_search', {'query':'Disabled'})['hits']


def test_revocation_invalidates_existing_connection_after_regrant(private):
    s, _ = private
    old = s.reader()
    old.call('recall_status', {})
    control.change(s, 'revoke')
    with pytest.raises(ValueError, match='revoked'): old.call('recall_status', {})
    control.change(s, 'grant')
    with pytest.raises(ValueError, match='revoked'): old.call('recall_status', {})
    assert s.reader().call('recall_search', {'query':'Publish'})['hits']
    assert control.read(s.path, s.owner)['capture'] == 'off'


def test_busy_reader_prevents_revocation_without_partial_change(private):
    s, _ = private
    with s.reader().open_connection():
        with pytest.raises(ValueError, match='active connections'): control.change(s, 'revoke')
    assert control.read(s.path, s.owner)['access'] == 'active'


def test_disclosure_is_exact_and_does_not_enable_shared_capture(private):
    s, _ = private
    hit = s.reader().call('recall_search', {'query':'Publish'})['hits'][0]
    selected = [{'block_id':hit['block_id'], 'start':0, 'end':13}]
    preview = control.disclosure_preview(s, selected)
    assert preview['excerpts'][0]['text'] == 'Publish this.'
    with pytest.raises(ValueError, match='changed'): control.disclose(s, selected, 'bad')
    receipt = control.disclose(s, selected, preview['fingerprint'])
    assert control.disclose(s, selected, preview['fingerprint'])['source'] == receipt['source']
    shared = RecallService(s.shared_path, s.repo_id)
    assert shared.call('recall_search', {'query':'Publish'})['hits']
    assert not shared.call('recall_search', {'query':'secret'})['hits']
    c = get_connection(s.shared_path)
    assert policy.mode(c, s.owner) == 'off'
    assert s.owner in policy.private_sources(c)
    assert c.execute('SELECT count(*) FROM memory_sources').fetchone()[0] == 1
    c.close()


@pytest.mark.parametrize('phase', ['revoked', 'purged'])
def test_interrupted_deletion_stays_revoked_and_retry_finishes(private, phase):
    s, trace = private
    preview = control.deletion_preview(s)
    def fault(at):
        if phase == at: raise RuntimeError('interrupt')
    with pytest.raises(RuntimeError): control.delete(s, preview['fingerprint'], fault=fault)
    with pytest.raises(ValueError, match='revoked'): s.reader().call('recall_status', {})
    assert s.capture(trace)['state'] == 'capture_disabled'
    control.delete(s, preview['fingerprint'])
    assert not any(x['rows'] for x in control.deletion_preview(s)['tables'].values())
    with pytest.raises(ValueError, match='Deleted'): control.change(s, 'grant')
    c = get_connection(s.shared_path)
    assert s.owner in policy.private_sources(c)
    c.close()


def test_stale_delete_preview_refused_before_revocation(private):
    s, trace = private
    preview = control.deletion_preview(s)
    write(trace, 'new material', True)
    s.capture(trace)
    with pytest.raises(ValueError, match='changed'): control.delete(s, preview['fingerprint'])
    assert s.reader().call('recall_search', {'query':'new material'})['hits']


@pytest.mark.parametrize('damage', ['missing', 'public', 'symlink', 'oversize', 'foreign'])
def test_unsafe_controls_refuse_reads_and_capture(private, damage):
    s, trace = private
    path = control.control_path(s.path)
    if damage == 'missing': path.unlink()
    elif damage == 'public': path.chmod(0o644)
    elif damage == 'symlink':
        saved = path.with_suffix('.saved'); path.rename(saved); path.symlink_to(saved)
    elif damage == 'oversize': path.write_text('x'*4097)
    else:
        data=json.loads(path.read_text());data['owner']='codex:foreign';path.write_text(json.dumps(data))
    with pytest.raises((ValueError,OSError)): s.reader().call('recall_status', {})
    with pytest.raises((ValueError,OSError)): s.capture(trace)


def test_restoring_content_cannot_undo_external_revocation(private, tmp_path):
    import sqlite3
    s, _ = private
    c=get_connection(s.path,private_owner=s.owner)
    backup=tmp_path/'before.db'; target=sqlite3.connect(backup);c.backup(target);target.close();c.close()
    control.change(s,'revoke')
    # Raw replacement is outside supported maintenance, but it cannot regrant
    # a private reader because its control record is independent of content.
    source=sqlite3.connect(backup); target=sqlite3.connect(s.path);source.backup(target);target.close();source.close()
    with pytest.raises(ValueError,match='revoked'):s.reader().call('recall_status',{})


def test_large_identity_line_is_bounded_before_capture(private):
    s, trace=private
    trace.write_bytes(b' '*(256*1024+1)+b'\n')
    with pytest.raises(ValueError,match='identity mismatch'):s.capture(trace)


def test_private_backup_restore_preserves_restrictions_and_exact_content(private, tmp_path):
    s,trace=private
    dest=tmp_path/"backup ?#%' quote.db"
    control.backup(s,dest)
    with pytest.raises(FileExistsError):control.backup(s,dest)
    assert dest.stat().st_mode & 0o777 == 0o600
    backup_bytes = dest.read_bytes()
    preview=control.restore_preview(s,dest)
    assert dest.read_bytes() == backup_bytes
    write(trace,'Later content',True);s.capture(trace)
    old=s.reader();old.call('recall_status',{})
    with pytest.raises(ValueError,match='changed'):control.restore(s,dest,'bad')
    control.restore(s,dest,preview['fingerprint'])
    assert dest.read_bytes() == backup_bytes
    with pytest.raises(ValueError,match='revoked'):old.call('recall_status',{})
    control.change(s,'grant')
    assert s.reader().call('recall_search',{'query':'Publish'})['hits']
    assert not s.reader().call('recall_search',{'query':'Later'})['hits']
    assert s.capture(trace)['state']=='capture_disabled'


def test_damaged_backup_refused_before_revocation(private,tmp_path):
    import sqlite3
    s,_=private;dest=tmp_path/'damaged.db';control.backup(s,dest)
    c=sqlite3.connect(dest);c.execute('DROP TRIGGER recall_private_memory_sources_insert');c.commit();c.close()
    with pytest.raises(ValueError,match='guards'):control.restore_preview(s,dest)
    assert control.read(s.path,s.owner)['access']=='active'


def test_deleted_owner_cannot_restore_old_backup(private,tmp_path):
    s,_=private;dest=tmp_path/'old.db';control.backup(s,dest)
    reviewed=control.restore_preview(s,dest)
    control.delete(s,control.deletion_preview(s)['fingerprint'])
    with pytest.raises(ValueError,match='Deleted'):control.restore(s,dest,reviewed['fingerprint'])


def test_private_rebuild_has_explicit_owner_publication(private):
    s,trace=private
    write(trace,'Rebuilt private evidence')
    assert s.capture(trace)['state']=='source_changed'
    result=s.capture(trace,rebuild=True)
    assert result['state']=='rebuild_ready'
    assert s.reader().call('recall_search',{'query':'Publish'})['hits']
    result=s.capture(trace,publish_rebuild=True)
    assert result['state']=='complete'
    assert s.reader().call('recall_search',{'query':'Rebuilt'})['hits']
