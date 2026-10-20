"""Global recall settings: a small JSON file, every key an explicit opt-in.

Default location: ~/.claude/context-recall/settings.json (RECALL_SETTINGS overrides,
which keeps tests away from the real file). Unknown keys are preserved.
"""
import json
import os
from pathlib import Path

DEFAULTS = {
    'codex_import': False,                       # import Codex rollouts at Claude session start
    'codex_sessions_dir': '~/.codex/sessions',   # where Codex writes rollout JSONL files
    'codex_import_seconds': 4.0,                 # time budget per session start
}
KEYS = tuple(DEFAULTS)


def path():
    return Path(os.environ.get('RECALL_SETTINGS') or Path.home() / '.claude' / 'context-recall' / 'settings.json')


def load():
    data = dict(DEFAULTS)
    try:
        stored = json.loads(path().read_text(encoding='utf-8'))
        if isinstance(stored, dict):
            data.update(stored)
    except (OSError, ValueError):
        pass
    return data


def save(data):
    p = path(); p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
    os.replace(tmp, p)


def set_value(key, raw):
    if key not in KEYS:
        raise ValueError('Unknown setting %r; known: %s' % (key, ', '.join(KEYS)))
    data = load()
    current = DEFAULTS[key]
    if isinstance(current, bool):
        if str(raw).lower() in ('on', 'true', '1', 'yes'):
            value = True
        elif str(raw).lower() in ('off', 'false', '0', 'no'):
            value = False
        else:
            raise ValueError('%s expects on/off' % key)
    elif isinstance(current, float):
        value = float(raw)
        if value <= 0:
            raise ValueError('%s must be positive' % key)
    else:
        value = str(raw)
    data[key] = value
    save(data)
    return data
