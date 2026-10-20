"""Test isolation: never let the suite touch the user's real recall data.

Leaks that have bitten: the event log hardcoded to ~/.claude (2.2.3), record_invocation()
opening the default DB (2.3.1), two hooks bypassing RECALL_DB (2.5.0 P31), and the v1
migration renaming ~/.claude/context-recall/index.json regardless of RECALL_DB (2.5.0 R03).
The environment below is set at conftest import time, before any test module imports
db.py (whose DB_DIR is computed from HOME once), and every inherited value is restored
when the session ends. Subprocess-based tests build their own environment on top of this.
"""
import os
import tempfile

import pytest

_TMP = tempfile.mkdtemp(prefix="recall-test-")
_KEYS = ("HOME", "RECALL_DB", "RECALL_LOG_FILE", "RECALL_SETTINGS", "CLAUDE_ENV_FILE",
         "CLAUDE_CODE_SESSION_ID", "RECALL_SESSION_ID", "RECALL_PROJECT_HASH")
_SAVED = {k: os.environ.get(k) for k in _KEYS}
os.environ["HOME"] = _TMP
os.environ["RECALL_DB"] = os.path.join(_TMP, "recall.db")
os.environ["RECALL_LOG_FILE"] = os.path.join(_TMP, "recall-events.log")
os.environ["RECALL_SETTINGS"] = os.path.join(_TMP, "settings.json")
for _k in ("CLAUDE_ENV_FILE", "CLAUDE_CODE_SESSION_ID", "RECALL_SESSION_ID", "RECALL_PROJECT_HASH"):
    os.environ.pop(_k, None)


@pytest.fixture(autouse=True, scope="session")
def _isolate_recall_store():
    """Keep the isolated environment for the whole session, then restore the caller's."""
    try:
        yield
    finally:
        for k, v in _SAVED.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
