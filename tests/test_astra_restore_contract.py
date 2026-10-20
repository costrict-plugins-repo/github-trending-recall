"""Astra's integration-verification tests on 6264d27 (P35/P47 boundaries): dropped runtime
column, dropped capture trigger, FTS index emptied with delete-all, doctor/restore consistency,
healthy control. Authored by Astra; only the repo root is made relative. Temporary stores only."""
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from db import get_connection
import memory_store as memory
from recall_memory import parser, run


def seed(conn, path, text):
    path.write_text(json.dumps({'type': 'user', 'uuid': 'a', 'sessionId': 'contract',
                    'timestamp': '2026-09-06', 'message': {'role': 'user', 'content': text}}) + '\n')
    memory.index_file(conn, path, session_id='contract', cwd=str(path.parent))
    conn.commit()


@pytest.mark.parametrize('damage', [
    'ALTER TABLE memory_blocks DROP COLUMN timestamp',
    'DROP TRIGGER memory_chunks_insert',
    "INSERT INTO memory_fts(memory_fts) VALUES('delete-all')",
])
def test_restore_rejects_unusable_backup_and_preserves_target(tmp_path, damage):
    target = get_connection(tmp_path / 'target.db')
    source = get_connection(tmp_path / 'backup.db')
    try:
        seed(target, tmp_path / 'target.jsonl', 'irreplaceabletargetsentinel')
        seed(source, tmp_path / 'source.jsonl', 'backupsearchsentinel')
        source.execute(damage)
        source.commit()
        assert source.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        source.close()
        rejected = False
        try:
            run(parser().parse_args(['restore', str(tmp_path / 'backup.db'), '--yes']), target)
        except (ValueError, sqlite3.DatabaseError):
            rejected = True
        assert rejected, 'Restore accepted the incomplete/inconsistent backup'
        assert memory.search(target, 'irreplaceabletargetsentinel')
    finally:
        source.close()
        target.close()


def test_doctor_detects_external_content_fts_mismatch(tmp_path):
    conn = get_connection(tmp_path / 'store.db')
    try:
        seed(conn, tmp_path / 'trace.jsonl', 'searchablesentinel')
        conn.execute("INSERT INTO memory_fts(memory_fts) VALUES('delete-all')")
        conn.commit()
        assert not memory.search(conn, 'searchablesentinel')
        report = run(parser().parse_args(['doctor']), conn)
        assert report['schema_check'] != 'ok', report
    finally:
        conn.close()


def test_healthy_backup_restores_and_searches(tmp_path):
    target = get_connection(tmp_path / 'target.db')
    source = get_connection(tmp_path / 'backup.db')
    try:
        seed(source, tmp_path / 'source.jsonl', 'backupsearchsentinel')
        source.close()
        run(parser().parse_args(['restore', str(tmp_path / 'backup.db'), '--yes']), target)
        assert memory.search(target, 'backupsearchsentinel')
    finally:
        source.close()
        target.close()

