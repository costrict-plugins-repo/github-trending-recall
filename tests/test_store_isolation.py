"""Every hook and script must honor RECALL_DB and never touch the default
~/.claude/context-recall store when it is overridden.

Found on 2026-09-05: session_end.py and post_compact.py passed DB_PATH
explicitly to get_connection(), bypassing the RECALL_DB check inside it. A
release smoke test with RECALL_DB pointed at a scratch file therefore opened
(and schema-migrated) the developer's real store. Each entry point runs here as
a subprocess with HOME set to an empty directory; nothing may appear there.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ['session_start', 'prompt_submit', 'stop', 'session_end', 'post_compact']
SCRIPTS = [
    ['fetch_exchanges.py', '--session', 'leak', 'last5'],
    ['show_index.py', '--session', 'leak'],
    ['manage_sessions.py', 'list'],
    ['recall_memory.py', 'status'],
    ['recall_memory.py', 'doctor'],
    ['recall_memory.py', 'brief'],
    ['recall_memory.py', 'search', 'leak'],
    ['recall_memory.py', 'sources'],
    ['recall_memory.py', 'config'],
]


def _record(role, text):
    return json.dumps({'type': role, 'timestamp': '2026-09-05T20:00:00Z',
                       'message': {'role': role, 'content': [{'type': 'text', 'text': text}]}})


LEGACY = json.dumps({'session_id': 'legacy-private', 'exchanges': []})


def _files_created_under_home(tmp_path, argv):
    """Run one entry point with the store redirected; return what changed under HOME.
    HOME is seeded with a sentinel and a v1 index.json: an entry point that reads,
    renames (R03) or writes anything there shows up as a difference."""
    home = tmp_path / 'home'
    (home / '.claude' / 'context-recall').mkdir(parents=True)
    (home / '.claude' / 'context-recall' / 'index.json').write_text(LEGACY)
    (home / 'sentinel').write_text('unchanged')
    before = {str(p.relative_to(home)): p.read_text() for p in home.rglob('*') if p.is_file()}
    transcript = tmp_path / 'tr.jsonl'
    transcript.write_text(_record('user', 'leak probe') + '\n' + _record('assistant', 'leak answer') + '\n')
    env = {**os.environ, 'HOME': str(home), 'RECALL_DB': str(tmp_path / 'isolated.db'),
           'CLAUDE_PLUGIN_ROOT': str(ROOT)}
    for key in ('RECALL_LOG_FILE', 'RECALL_SETTINGS', 'CLAUDE_CODE_SESSION_ID'):
        env.pop(key, None)
    payload = json.dumps({'session_id': 'leak', 'cwd': str(tmp_path), 'transcript_path': str(transcript),
                          'prompt': 'leak probe', 'last_assistant_message': 'leak answer', 'trigger': 'compact'})
    # Read-only entry points now require an existing store. Seed the redirected
    # fixture explicitly; retain the HOME sentinel checks around the actual call.
    from db import get_connection
    seed = get_connection(tmp_path / 'isolated.db')
    seed.close()
    result = subprocess.run([sys.executable, *argv], input=payload, capture_output=True, text=True,
                            cwd=str(ROOT), env=env, timeout=60)
    assert result.returncode == 0, (argv, result.stderr[-500:])
    after = {str(p.relative_to(home)): p.read_text() for p in home.rglob('*') if p.is_file()}
    return sorted(set(after.items()) ^ set(before.items()))


@pytest.mark.parametrize('hook', HOOKS)
def test_hook_honors_recall_db(tmp_path, hook):
    assert _files_created_under_home(tmp_path, [str(ROOT / 'hooks' / f'{hook}.py')]) == []


@pytest.mark.parametrize('argv', [a for a in SCRIPTS if (ROOT / 'scripts' / a[0]).exists()],
                         ids=lambda a: ' '.join(a))
def test_script_honors_recall_db(tmp_path, argv):
    assert _files_created_under_home(tmp_path, [str(ROOT / 'scripts' / argv[0]), *argv[1:]]) == []
