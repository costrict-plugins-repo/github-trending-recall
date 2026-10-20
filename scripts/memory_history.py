"""Explicit offline conversation imports. No account access or automatic discovery.

Provider adapters accept narrowly validated JSON shapes, not a promise that all
provider export versions have these shapes. Unknown formats fail before writes.
"""
import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import memory_store as memory
from memory_transfer import import_source

MAX_BYTES = 32 * 1024 * 1024


def _load(path, max_bytes=MAX_BYTES):
    path=Path(path)
    if type(max_bytes) is not int or not 1<=max_bytes<=128*1024*1024:
        raise ValueError('max_bytes must be 1..134217728')
    payloads=[]
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            members=[i for i in archive.infolist() if re.fullmatch(r'conversations(?:[-_]\d+)?\.json',Path(i.filename).name)]
            if not members:raise ValueError('No conversations JSON in archive; no files were extracted')
            if len({i.filename for i in members})!=len(members):raise ValueError('Duplicate conversation ZIP member')
            if sum(i.file_size for i in members)>max_bytes:raise ValueError('Conversation JSON exceeds byte budget')
            for member in members:
                with archive.open(member) as stream:
                    raw=stream.read(max_bytes+1)
                if len(raw)>max_bytes:raise ValueError('Conversation JSON exceeds byte budget')
                payloads.append(json.loads(raw))
    else:
        with path.open('rb') as stream:raw=stream.read(max_bytes+1)
        if len(raw)>max_bytes:raise ValueError('Conversation JSON exceeds byte budget')
        payloads=[json.loads(raw)]
    conversations=[]
    for data in payloads:
        if isinstance(data,list):conversations.extend(data)
        elif isinstance(data,dict) and data.get('format')=='recall-chat-snapshot-v1':conversations.append(data)
        else:raise ValueError('Expected a conversation array or recall-chat-snapshot-v1')
    seen=set()
    for c in conversations:
        provider,cid=identity(c)
        if (provider,cid) in seen:raise ValueError('Duplicate conversation identity across files')
        seen.add((provider,cid))
    return conversations


def identity(c):
    if not isinstance(c,dict):raise ValueError('Conversation must be an object')
    if c.get('format')=='recall-chat-snapshot-v1':
        provider='app-snapshot';cid=c.get('id')
        if c.get('surface') not in ('claude-chat','claude-cowork','chatgpt'):
            raise ValueError('Snapshot needs an explicit supported surface')
        if not isinstance(c.get('provenance'),str) or not c['provenance'].strip():
            raise ValueError('Snapshot needs a provenance description; do not substitute a model summary')
    elif isinstance(c.get('chat_messages'),list):provider='claude-export';cid=c.get('uuid')
    elif isinstance(c.get('mapping'),dict):provider='chatgpt-export';cid=c.get('id') or c.get('conversation_id')
    else:raise ValueError('Unrecognized conversation shape; nothing imported')
    if not isinstance(cid,str) or not cid.strip() or len(cid)>256:raise ValueError('Invalid conversation id')
    return provider,cid


def stamp(value):
    if value is None:return ''
    if isinstance(value,str):return value
    if type(value) in (int,float):
        try:return datetime.fromtimestamp(value,timezone.utc).isoformat()
        except (ValueError,OverflowError,OSError):pass
    raise ValueError('Invalid message timestamp')


def messages(c):
    provider,_=identity(c); skipped=0; inactive=0; output=[]
    if provider=='claude-export':
        raw=c['chat_messages']
    elif provider=='app-snapshot':
        raw=c.get('messages')
        if not isinstance(raw,list):raise ValueError('Snapshot messages must be an array')
    else:
        mapping=c['mapping'];node=c.get('current_node'); chain=[];seen=set()
        if not isinstance(node,str) or node not in mapping:raise ValueError('ChatGPT current_node required; no branch guessed')
        while node is not None:
            if node in seen or node not in mapping:raise ValueError('Broken or cyclic ChatGPT parent chain')
            seen.add(node);entry=mapping[node]
            if not isinstance(entry,dict):raise ValueError('Invalid ChatGPT node')
            msg=entry.get('message')
            if msg is not None:chain.append(msg)
            node=entry.get('parent')
            if node is not None and not isinstance(node,str):raise ValueError('Invalid parent id')
        raw=list(reversed(chain));inactive=len(mapping)-len(seen)
    keys=set()
    for msg in raw:
        if not isinstance(msg,dict):raise ValueError('Invalid message object')
        if provider=='chatgpt-export':
            author=msg.get('author',{})
            if not isinstance(author,dict):raise ValueError('Invalid message author')
            role=author.get('role');key=msg.get('id');when=msg.get('create_time')
            meta=msg.get('metadata') or {}
            if not isinstance(meta,dict):raise ValueError('Invalid message metadata')
            if role not in ('user','assistant') or meta.get('is_visually_hidden_from_conversation') or msg.get('channel') in ('analysis','commentary') or msg.get('recipient') not in (None,'all'):
                skipped+=1;continue
            content=msg.get('content')
            if not isinstance(content,dict) or content.get('content_type') not in ('text','multimodal_text'):
                skipped+=1;continue
            parts=content.get('parts')
            if not isinstance(parts,list):raise ValueError('Invalid ChatGPT text parts')
            texts=[p for p in parts if isinstance(p,str)]
            skipped+=sum(not isinstance(p,str) for p in parts)
        else:
            role=msg.get('sender') if provider=='claude-export' else msg.get('role')
            role={'human':'user'}.get(role,role);key=msg.get('uuid') if provider=='claude-export' else msg.get('id')
            when=msg.get('created_at') if provider=='claude-export' else msg.get('timestamp')
            if role not in ('user','assistant') or msg.get('isMeta') or msg.get('isCompactSummary'):
                skipped+=1;continue
            content=msg.get('content',msg.get('text'))
            if content==[] and isinstance(msg.get('text'),str):content=msg['text']
            skipped+=sum(len(msg.get(k,[])) for k in ('attachments','files') if isinstance(msg.get(k,[]),list))
            if isinstance(content,str):texts=[content]
            elif isinstance(content,list):
                texts=[]
                for part in content:
                    if isinstance(part,dict) and part.get('type')=='text' and isinstance(part.get('text'),str):texts.append(part['text'])
                    else:skipped+=1
            else:raise ValueError('Unknown text content shape')
        if not isinstance(key,str) or not key or key in keys:raise ValueError('Missing or duplicate message identity')
        keys.add(key)
        for ordinal,text in enumerate(texts):
            if text:output.append((key,role,stamp(when),ordinal,text))
    return output,{'excluded_items':skipped,'inactive_branch_nodes':inactive,'retained_text_blocks':len(output)}


def preview(conversations):
    result=[]
    for c in conversations:
        provider,cid=identity(c)
        # Validate before listing an importable conversation. No message text printed.
        _,coverage=messages(c)
        result.append({'provider':provider,'id':cid,'title':c.get('title',c.get('name','')),**coverage})
    return {'conversations':result,'notice':'Preview only. Import exactly one id into an explicit repository. Text only; attachments, tool results and inactive ChatGPT branches are not imported.'}


def ingest(conn,path,provider,cid,cwd,max_bytes=MAX_BYTES):
    if not Path(cwd).is_dir():raise ValueError('Repository scope directory must exist')
    conversations=load(path,max_bytes)
    selected=[c for c in conversations if identity(c)==(provider,cid)]
    if len(selected)!=1:raise ValueError('Select exactly one listed provider and conversation id')
    c=selected[0];turns,coverage=messages(c)
    if not turns:raise ValueError('Selected conversation has no supported visible text')
    key=provider+':'+cid
    source={'source_key':key,'session_id':cid,'agent':provider,'path':str(Path(path).resolve()),
            'project_path':str(Path(cwd).resolve()),'repo_id':memory.repository_identity(cwd)}
    blocks=[]
    for seq,(mid,role,when,ordinal,text) in enumerate(turns):
        bid=memory.digest(f'{key}:{mid}:{ordinal}:text')[:32]
        blocks.append({'id':bid,'source_key':key,'message_key':mid,'seq':seq,'role':role,'kind':'text',
                       'timestamp':when,'text':text,'start_byte':0,'end_byte':0,
                       'content_hash':memory.digest(text),'ordinal':ordinal,'generation':0})
    # import_source uses a savepoint and validates every row before target writes.
    conn.execute('SAVEPOINT recall_history')
    try:
        result=import_source(conn,{'format':'recall-blocks-v1','source':source,'blocks':blocks})
        wanted={b['id'] for b in blocks}
        import memory_revisions
        for row in conn.execute('SELECT * FROM memory_blocks WHERE source_key=?',(key,)).fetchall():
            if row['id'] not in wanted:
                memory_revisions.remember(conn,row);conn.execute('DELETE FROM memory_blocks WHERE id=?',(row['id'],))
        conn.execute("UPDATE memory_sources SET state='imported_snapshot',last_indexed_at=?,error=?,scope_pinned=1 WHERE source_key=?",(datetime.now(timezone.utc).isoformat(),json.dumps({'coverage':coverage,'provenance':c.get('provenance','User-selected provider export; text only'),'surface':c.get('surface',provider),'source_url':c.get('url')}),key))
        conn.execute('RELEASE recall_history')
    except BaseException:
        conn.execute('ROLLBACK TO recall_history');conn.execute('RELEASE recall_history');raise
    result.update(coverage=coverage,state='imported_snapshot',note='Selected visible text snapshot replaced atomically; old text follows revision retention. No automatic refresh or capture. Offsets refer to retained text, not export file bytes.')
    return result


def load(path,max_bytes=MAX_BYTES):
    try:return _load(path,max_bytes)
    except (zipfile.BadZipFile,zipfile.LargeZipFile,NotImplementedError,RuntimeError,UnicodeError) as exc:
        raise ValueError('Cannot read supported conversation JSON: '+str(exc)) from exc
