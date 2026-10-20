#!/usr/bin/env python3
"""Owner-only private Recall maintenance. No command here is a model tool.

Privacy separates supported Recall readers, not arbitrary processes with the same
OS account. Stop obsolete clients before converting a live shared source.
"""
import argparse
import json
import sqlite3
import math
import time
import sys
from pathlib import Path

from db import get_connection
import memory_store as memory
import recall_conversion as conversion
import recall_private_control as controls
from recall_private_store import SessionStore


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--shared-db', type=Path, required=True)
    p.add_argument('--directory', type=Path, required=True, help='Dedicated directory for private stores')
    p.add_argument('--owner', required=True, help='Exact native root agent:session identity')
    p.add_argument('--cwd', type=Path, required=True)
    commands = p.add_subparsers(dest='command', required=True)
    commands.add_parser('create', help='New source only; existing history requires conversion')
    commands.add_parser('preview-conversion')
    convert = commands.add_parser('convert')
    convert.add_argument('--reviewed', required=True, help='Fingerprint returned by preview-conversion')
    commands.add_parser('recover-conversion')
    commands.add_parser('status')
    index = commands.add_parser('index'); index.add_argument('--transcript',type=Path,required=True)
    index.add_argument('--seconds',type=float,default=10)
    index.add_argument('--rebuild',action='store_true')
    backup = commands.add_parser('backup'); backup.add_argument('--output',type=Path,required=True)
    for name in ('preview-restore','restore'):
        restore = commands.add_parser(name); restore.add_argument('--input',type=Path,required=True)
        if name == 'restore': restore.add_argument('--reviewed',required=True)
    for action in ('pause', 'revoke', 'grant'):
        commands.add_parser(action)
    resume = commands.add_parser('resume-capture')
    resume.add_argument('--allow-backfill', action='store_true')
    commands.add_parser('preview-delete')
    delete = commands.add_parser('delete')
    delete.add_argument('--reviewed', required=True)
    for name in ('preview-share', 'share'):
        cmd = commands.add_parser(name)
        cmd.add_argument('--selection', type=Path, required=True,
                         help='JSON array of exact {block_id,start,end} selections')
        if name == 'share': cmd.add_argument('--reviewed', required=True)
    return p


def run(args):
    conversion._validate_owner(args.owner)
    directory = args.directory.expanduser().resolve()
    store = SessionStore(directory/(memory.digest(args.owner)+'.db'), args.owner,
                         memory.repository_identity(args.cwd), args.shared_db)
    if args.command == 'create':
        conn = get_connection(args.shared_db)
        try: store = SessionStore.create(conn, directory, args.owner, args.cwd)
        finally: conn.close()
        return {'created': str(store.path), 'owner': store.owner, 'native_session_started': False}
    if args.command == 'preview-conversion':
        return conversion.preview(args.shared_db, args.owner, args.cwd)
    if args.command == 'convert':
        store = conversion.convert(args.shared_db, directory, args.owner, args.cwd,
                                   expected_fingerprint=args.reviewed)
        return {'converted': str(store.path), 'owner': store.owner}
    if args.command == 'recover-conversion':
        # Refuse unrelated recovery before touching the recorded transition.
        record = conversion.load_record(conversion.gate_path(args.shared_db))
        if record['owner'] != args.owner or record['target'] != str(store.path) or record['repo_id'] != store.repo_id:
            raise ValueError('Recovery does not match the selected owner/destination/repository')
        store = conversion.recover(args.shared_db)
        return {'recovered': str(store.path), 'owner': store.owner}
    if args.command == 'backup': return controls.backup(store,args.output)
    if args.command == 'preview-restore': return controls.restore_preview(store,args.input)
    if args.command == 'restore': return controls.restore(store,args.input,args.reviewed)
    if args.command == 'index':
        if not math.isfinite(args.seconds) or not 0 < args.seconds <= 300:
            raise ValueError('Private index budget must be in (0, 300] seconds')
        start=time.monotonic(); result=None; passes=0
        while time.monotonic()-start < args.seconds:
            result=store.capture(args.transcript,publish_rebuild=True,rebuild=args.rebuild and passes==0); passes+=1
            if result['state'] not in ('backlog','rebuilding','rebuild_ready'): break
        return {'passes':passes,'result':result,'seconds':round(time.monotonic()-start,4)}
    if args.command == 'status':
        result = controls.deletion_preview(store)
        state = controls.read(store.path, store.owner)
        return {**result, 'access': state['access'], 'capture': state['capture']}
    if args.command in ('pause', 'revoke', 'grant', 'resume-capture'):
        return controls.change(store, 'resume' if args.command == 'resume-capture' else args.command,
                               allow_backfill=getattr(args, 'allow_backfill', False))
    if args.command == 'preview-delete': return controls.deletion_preview(store)
    if args.command == 'delete': return controls.delete(store, args.reviewed)
    with args.selection.open('rb') as stream: raw = stream.read(65537)
    if len(raw) > 65536: raise ValueError('Selection exceeds 64 KiB')
    selected = json.loads(raw)
    if args.command == 'preview-share': return controls.disclosure_preview(store, selected)
    return controls.disclose(store, selected, args.reviewed)


def main():
    try:
        print(json.dumps(run(parser().parse_args()), indent=2, ensure_ascii=False)); return 0
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({'error': str(exc)}), file=sys.stderr); return 1


if __name__ == '__main__': raise SystemExit(main())
