"""Validated source-scoped portable exports; staged rebuilds are never exported."""
import re

import memory_store as memory
import memory_revisions as revisions
from utils import redact_secrets


def export_source(conn, source_key, include_revisions=False):
    row=conn.execute('SELECT * FROM memory_sources WHERE source_key=?',(source_key,)).fetchone()
    if row is None:
        raise ValueError('Unknown source: '+source_key)
    result={'format':'recall-blocks-v2' if include_revisions else 'recall-blocks-v1',
            'source':revisions.published_source(row),
            'blocks':[dict(r) for r in conn.execute('SELECT * FROM memory_blocks WHERE source_key=? ORDER BY seq,ordinal,id',(source_key,))],
            'unpublished_rebuild_excluded':bool(row['rebuild_in_progress']),
            'includes_revisions':include_revisions}
    from recall_privacy import export_policy
    result['capture_policy'] = export_policy(conn, source_key)
    if include_revisions:
        result['revisions']=[dict(r) for r in conn.execute('SELECT * FROM memory_revisions WHERE source_key=? ORDER BY revision_order',(source_key,))]
    return result


def validate(data):
    if not isinstance(data,dict) or data.get('format') not in ('recall-blocks-v1','recall-blocks-v2'):
        raise ValueError('Not a recall-blocks-v1/v2 export')
    policy=data.get('capture_policy', {'version':1,'mode':'shared'})
    if not isinstance(policy,dict) or policy.get('version')!=1 or policy.get('mode') not in ('shared','off'):
        raise ValueError('Unsupported capture policy in export')
    src=data.get('source')
    if not isinstance(src,dict) or any(not isinstance(src.get(k),str) or not src[k] for k in ('source_key','session_id','agent','path','project_path','repo_id')):
        raise ValueError('Export has invalid source metadata')
    if src['source_key']!=src['agent']+':'+src['session_id'] and not src['source_key'].startswith(src['agent']+':'+src['session_id']+'/'):
        raise ValueError('Export source identity disagrees with agent/session')
    if not isinstance(data.get('blocks'),list) or not isinstance(data.get('revisions',[]),list):
        raise ValueError('Export blocks/revisions must be arrays')
    if data.get('revisions') and data['format']!='recall-blocks-v2':
        raise ValueError('Revision history requires recall-blocks-v2')
    seen=set(); versions=set()
    for historical,items in ((False,data['blocks']),(True,data.get('revisions',[]))):
        for block in items:
            if not isinstance(block,dict):raise ValueError('Invalid export block')
            for key in ('id','message_key','role','kind','timestamp','text','content_hash'):
                if not isinstance(block.get(key),str):raise ValueError('Invalid export block '+key)
            for key in ('seq','start_byte','end_byte','ordinal','generation'):
                value=block.get(key,0)
                if isinstance(value,bool) or not isinstance(value,int) or value<0:raise ValueError('Invalid export block '+key)
            if block['role'] not in ('user','assistant','host') or block['kind'] not in ('text','tool_use'):
                raise ValueError('Unsupported export role/kind')
            if block.get('source_key')!=src['source_key']:
                raise ValueError('Export contains a foreign source block')
            expected=memory.digest(f"{src['source_key']}:{block['message_key']}:{block.get('ordinal',0)}:{block['kind']}")[:32]
            if block['id']!=expected or memory.digest(block['text'])!=block['content_hash']:
                raise ValueError('Export block identity or content hash does not match its text')
            identity=(block['id'],block['content_hash'])
            if historical:
                if identity in versions:raise ValueError('Duplicate historical revision')
                versions.add(identity)
                if block.get('pinned',0) not in (0,1):raise ValueError('Invalid revision pin')
            else:
                if block['id'] in seen:raise ValueError('Duplicate current block')
                seen.add(block['id'])
    return src


def import_source(conn,data):
    # Imported suppression is restrictive owner state, never a sharing grant.
    # Hold the same control lock as restore/mode changes until it is durable.
    from recall_privacy import journal_lock
    with journal_lock(conn):
        return _import_source(conn,data)


def _import_source(conn,data):
    src=validate(data);key=src['source_key']
    from recall_privacy import require_capture
    require_capture(conn, key)
    existing_source=conn.execute('SELECT * FROM memory_sources WHERE source_key=?',(key,)).fetchone()
    if existing_source and (existing_source['repo_id']!=src['repo_id'] or existing_source['rebuild_in_progress']):
        raise ValueError('Existing source has a different scope or an unfinished rebuild; import refused')
    # Validate every identity collision before any target write.
    for block in data['blocks']+data.get('revisions',[]):
        collision=conn.execute('SELECT source_key FROM memory_blocks WHERE id=? UNION SELECT source_key FROM memory_revisions WHERE id=?',(block['id'],block['id'])).fetchall()
        if any(row[0]!=key for row in collision):raise ValueError('Export block collides with a foreign source')
    conn.execute('SAVEPOINT recall_import')
    try:
        conn.execute('''INSERT INTO memory_sources(source_key,session_id,agent,path,project_path,repo_id,state)
            VALUES(?,?,?,?,?,?,'source_missing') ON CONFLICT(source_key) DO NOTHING''',
            tuple(src[k] for k in ('source_key','session_id','agent','path','project_path','repo_id')))
        generation=conn.execute('SELECT generation FROM memory_sources WHERE source_key=?',(key,)).fetchone()[0]
        redacted=loaded=0
        for historical,items in ((True,data.get('revisions',[])),(False,data['blocks'])):
            for original in items:
                b={k:original.get(k,0) for k in revisions.FIELDS}
                b['generation']=generation
                text=redact_secrets(re.sub(r'data:[^\s,]*;base64,[A-Za-z0-9+/=]+','[ELIDED:base64]',b['text']))
                redacted+=int(text!=b['text']);b['text']=text;b['content_hash']=memory.digest(text)
                if historical:
                    revisions.remember(conn,b,pinned=bool(original.get('pinned')),trim=False)
                else:
                    loaded+=memory._put_block(conn,b)
        for bid in {b['id'] for b in data.get('revisions',[])}:
            revisions.collect(conn,bid)
        conn.execute("UPDATE memory_sources SET state='source_missing',byte_offset=0,tail_size=0,head_hash=NULL WHERE source_key=?",(key,))
        if data.get('capture_policy',{}).get('mode') == 'off':
            from recall_privacy import journal_state, write_journal
            write_journal(conn, journal_state(conn) | {key})
            conn.execute("INSERT INTO recall_capture_policy VALUES(?, 'off', datetime('now')) ON CONFLICT(source_key) DO UPDATE SET mode='off'",(key,))
        conn.execute('RELEASE recall_import')
    except BaseException:
        conn.execute('ROLLBACK TO recall_import');conn.execute('RELEASE recall_import');raise
    return {'source':key,'blocks_loaded':loaded,'blocks_in_file':len(data['blocks']),
            'historical_revisions_in_file':len(data.get('revisions',[])),'redacted_versions':redacted,
            'note':'Current blocks merged; historical revisions respect the target retention policy, pins preserved. Source is marked source_missing until its transcript is indexed again.'}
