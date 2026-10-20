# Install or update Recall 2.5

Recall 2.5 uses **store schema 12**. Install the code first, migrate the intended
store once, then refresh every client that reads it. Restarting an app before
installing the new files can leave it running the old reader. The version label
alone does not prove which code an already-running process loaded.

These instructions apply to the 2.5 release when published. Until then, a public
marketplace update may still return 2.4. The community catalog is commit-pinned
and can lag the Recall marketplace. Do not uninstall a marketplace to refresh it.

## Choose the surfaces you use

| Surface | Install | Apply an update | Capture |
|---|---|---|---|
| Claude Code | `recall@recall-local` from the Recall marketplace | Update marketplace/plugin, then `/reload-plugins` in existing sessions, or start a new session | Plugin hooks retain selected conversation content |
| Codex skill | Run `install-codex-skill` from a stable Recall directory | Rerun installer from the new directory/version; start a context that discovers the refreshed skill | Explicit import or foreground watcher; installing a skill starts neither |
| Codex MCP | Add one repository-scoped stdio server | Update its script path if needed, then Settings → MCP servers → Restart; restart the client if its loaded connection remains stale | Separate capture process |
| Claude Desktop Chat | Generated Desktop MCP configuration | Regenerate the reader, update its configuration/path, quit and reopen Claude | Deliberate import/snapshot; no automatic Chat capture |
| Cowork connected to a Mac | Generated machine-specific plugin ZIP | Upload the new ZIP under the same plugin name, choose Replace, keep its MCP component enabled, quit/reopen Claude after active tasks finish, then use a fresh task | Explicit selected local transcript import/watch, where available |

Official host instructions: [Claude Code plugin updates and reload](https://code.claude.com/docs/en/discover-plugins),
[Codex MCP configuration and Restart](https://learn.chatgpt.com/docs/extend/mcp).
Exact menus vary by host version. The MCP Settings option was not visible to the
maintainer in the tested Codex desktop build; in that case use a full client restart
after a safe task boundary. Do not assume an agent can operate Codex itself: its
Computer Use guard may prohibit that, requiring the user to perform the restart. Reloading Claude Code does not reload Codex or
replace a Cowork upload. A fresh Python process seeing new capture commits does
not imply an existing Python process reloaded its modules.

## New installation

1. For Claude Code, register and install the marketplace package:

   ```sh
   claude plugin marketplace add https://github.com/bledden/claude-recall-marketplace
   claude plugin install recall@recall-local
   ```

   Run `/reload-plugins` in an existing Claude Code session, or start a fresh one.
   Check `/recall:recall` is available. New hook activity initializes the default
   store at `~/.claude/context-recall/recall.db`. Existing closed histories are not
   assumed to be indexed merely because the plugin is installed.

2. For Codex or app-only use, keep the unpacked release or checkout at a stable
   absolute path. Below, `/path/to/recall` is that directory (it contains
   `scripts/recall_memory.py`), not a temporary ZIP preview. Choose an explicit
   database and one actual repository. Python 3.9+ with SQLite FTS5 is required;
   lexical Recall needs no third-party Python package or model download.

   Import an actual selected transcript to create/populate the store:

   ```sh
   python3 /path/to/recall/scripts/recall_memory.py --db /path/to/recall.db \
     index /path/to/selected-session.jsonl --agent claude --cwd /path/to/project
   # Use --agent codex for a Codex rollout file.
   python3 /path/to/recall/scripts/recall_memory.py --db /path/to/recall.db doctor
   python3 /path/to/recall/scripts/recall_memory.py --db /path/to/recall.db \
     search "a distinctive remembered phrase" --cwd /path/to/project
   ```

   `--db` goes before the subcommand. Parent directories for the database are
   created by the writer. Reads and MCP never create or migrate a missing store.
   Repeat timed imports without `--rebuild` until complete. Check returned scope
   and coverage; an empty search is not proof that no earlier work exists.

3. Add only the readers you need:
   - **Codex skill:** `python3 /path/to/recall/scripts/recall_memory.py install-codex-skill`.
     This writes `~/.agents/skills/recall/SKILL.md` and points it at that script.
     It uses the default store or `RECALL_DB`; the installer does **not** persist
     an explicit `--db` into the skill. For a custom store, configure the host's
     `RECALL_DB` or use the explicit-db MCP route. Back up any customized existing
     skill before rerunning the installer. `--skills-dir` supports older hosts.
   - **Codex MCP:** follow [cross-agent setup](gpt-expansion.md), with explicit
     `--db` and repository `--cwd`/`--repo-id`. Keep that scope in the host config.
   - **Desktop/Cowork:** follow [Claude app setup](claude-app.md). The preparer
     requires already-indexed sources in the selected scope. Its private ZIP
     contains machine-specific launch paths, not history; it is different from
     the public Code-plugin release ZIP. Prepare a separate reader per repository.

4. Run the acceptance check below in each surface you installed. No embedding
   model, capture watcher, diagnostic logger or account-history importer is
   enabled by these reader setup steps.

## Existing users: coordinated upgrade

Arrange a short maintenance boundary for clients sharing the database. Finish
or pause Recall-dependent calls and capture/watch activity you control. You need
not finish the entire project. Avoid letting an old writer or reader run during
migration; preserve its configuration so it can restart afterward.

### 1. Back up before the first new writer runs

Use SQLite's backup API so committed WAL data is included. Do not copy just the
`.db` file while writers may be using it. This works even for a 2.4 store that
predates Recall's backup command. Set the two paths to your actual source and a
new backup filename, then run on the machine holding the store:

```sh
python3 - /path/to/recall.db /path/to/recall-before-2.5.db <<'PY'
import sqlite3, sys
from pathlib import Path
src = Path(sys.argv[1]).expanduser().resolve(strict=True)
dst = Path(sys.argv[2]).expanduser().absolute()
with dst.open('xb'):
    pass
with sqlite3.connect(src.as_uri() + '?mode=ro', uri=True) as source:
    with sqlite3.connect(dst) as target:
        source.backup(target)
        assert target.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
print('Backup verified:', dst)
PY
```

Keep copies of the old package, customized skill, MCP configuration and generated
reader folders too. The backup must finish successfully before migration.

### 2. Install the new code, then migrate once

For the recommended Claude Code marketplace:

```sh
claude plugin marketplace update recall-local
claude plugin update recall@recall-local
```

For a release ZIP/checkout, unpack/update the intended stable installation and
verify the new package is the one your commands reference. Use the same
marketplace and install scope as before; inspect `/plugin` if a catalog pin still
serves an older version. A local pre-release test is not proof a public update is
available yet.

Before resuming capture or model calls, run the **new** maintenance command:

```sh
python3 /path/to/new-recall/scripts/recall_memory.py --db /path/to/recall.db doctor
```

It migrates and checks the store; require `sqlite_check: "ok"`, `fts_check: "ok"`,
and `schema_check: "ok"`. Confirm schema 12 with the read-only version command
in Troubleshooting below (doctor does not return a schema-version field). Doctor requires
write access and records an invocation even without `--repair`. A new capture
hook can also migrate automatically; the explicit sequence above lets you check
it before reconnecting all clients. Do not run `--repair` just to clear a stale
reader error.

Migration preserves available legacy and durable data. It does not reconstruct
previously discarded text or automatically import all old sessions. Original
transcripts are needed for full durable backfill. Existing schema-9 sources do
not need a blanket rebuild merely to upgrade to schema 12.

### 3. Update every reader copy and refresh its loaded process

- **Claude Code:** `/reload-plugins` in sessions with older loaded plugin text,
  or use a new session. Point any explicit MCP configuration to the new code.
- **Codex:** rerun the skill installer if used; verify the MCP command points to
  the new runtime. Select MCP Restart in Settings and then call `recall_status`
  from the actual task. If it still uses old code, restart the client at a safe
  task boundary. A successful separate CLI probe does not close this check.
- **Claude Desktop and Cowork:** rerun `prepare_claude_app.py` from the new version,
  into a **new final directory**, retaining the previous `--name`, database,
  repository scope, Python choice and any deliberately enabled diagnostics.
  Review the new Desktop snippet and replace only that reader's entry, preserving
  other settings. Keep its unpacked directory in place. Quit/reopen Claude.
  Separately upload the new generated ZIP in Customize → Plugins, choose
  **Replace** for the same name, and keep the local MCP component enabled.
  After all uploads are replaced and running tasks finish, quit/reopen Claude
  again if needed, then use a fresh Mac-connected Cowork task. Replacement alone
  retained old reader processes in the tested app; a fresh task alone did not
  reload them. Use the generated ZIP directly, with its manifest at archive root.
  Changing Desktop's directory does not
  replace the already-uploaded plugin. Do this for each configured repository.

Resume the explicitly configured capture processes afterward. If a source needs
rebuild, use `index FILE --agent AGENT --cwd PROJECT --rebuild --seconds 120` with
an appropriate budget. If still `rebuilding`/`rebuild_ready`, repeat **without**
`--rebuild` to finish. Until explicit publication, that source's search and
compaction recovery retain its previous published state. Hooks stage subsequent
turns but do not publish that rebuild. See [revision behavior](revision-evidence-design.md).

### 4. Verify the client, not just the version label

In each configured surface:

1. Call `recall_status`. Confirm the intended repository and expected source
   coverage, with no schema error. This is scoped; CLI `status` is global.
2. Search a known distinctive phrase with `limit: 1`.
3. Get that hit using its `block_id`, **`start: start_char`**, `revision: content_hash`,
   and a window large enough for the short exact `quote` (the default 2,000-character
   get window covers a normal search passage; a 200-character window may not). Require
   `revision_requested: true` and `citation_check.valid: true`; cite the returned
   `quote_start`/`quote_end`. Offset 0 can exclude a quote found deep inside a block.
4. Check a known foreign repository block is refused by a scoped reader. Confirm
   a later separately captured synthetic record is visible without reindexing
   through the reader. Remove test sources only if deliberately created for testing.

These checks confirm connection, scope and citation mechanics, not model answer
quality or automatic capture of the current chat. Retained revision hashes refer
to redacted text; unpinned older revisions may expire under the retention policy.

## Troubleshooting after update

| Symptom | Check and action |
|---|---|
| Schema mismatch in every new process | Compare installed runtime and actual database path. Run the new doctor's explicit migration once if the store is older. |
| New doctor and fresh CLI pass, existing MCP still says schema differs | Its loaded code or generated copy is stale. Replace that copy and reconnect the host. Do not repeatedly migrate an already-current store. |
| New CLI also expects an older schema | The command points to an old checkout/cache. Correct the path and reinstall/refresh the intended version. |
| Missing store / `store_missing` | Check `--db`/`RECALL_DB` and intended capture/import. Readers do not create stores. |
| `store_access` / SQLite sidecar permission error | Retry the same read/scope through the host's normal approval mechanism. Do not use an immutable snapshot or change permissions to hide the error. |
| Plugin installed but no tools in this surface | Verify its MCP component, connected Mac and fresh task. Desktop config and Cowork upload are separate. |
| Correct tools, wrong repository | Check fixed launch scope. A chat title or folder switch does not rescope the server. |
| Citation check false | Check the returned window, exact quote and revision hash; correct or withdraw the quote. This does not by itself mean the index is broken. |

To inspect the store version without opening a writer:

```sh
python3 - /path/to/recall.db <<'PY'
import sqlite3, sys
from pathlib import Path
p = Path(sys.argv[1]).expanduser().resolve(strict=True)
with sqlite3.connect(p.as_uri() + '?mode=ro', uri=True) as c:
    print('Store schema:', c.execute('PRAGMA user_version').fetchone()[0])
PY
```

Compare it with `SCHEMA_VERSION = 12` in the `scripts/db.py` actually configured
for the reader. Older readers may suggest doctor for either direction of mismatch.
An MCP protocol `serverInfo.version` or plugin display label is not a schema check.

## Rollback

If capture policies or private sessions are active, **do not downgrade to the old
runtime or restore an old shared store without the current suppression journal**.
Older schema-10 writers do not implement these protections. Keep the matching
current runtime for supported restore, or leave the store stopped. See
[private-session recovery](private-sessions.md#backups-updates-and-rollback).
The ordinary pre-upgrade rollback below applies only before privacy is enabled.


Keep the pre-upgrade backup and old installation together. Downgrading files alone
does not undo schema 12 and can break old readers. Stop all consumers before a
rollback, preserve a fresh backup of the current store, and restore the
pre-upgrade store with its matching old runtime. Changes captured after that
backup are not in it. Recall's current `restore` validates and migrates the staging
copy to the current schema; it is a recovery command, **not a schema downgrade**.
Do not delete the only copy of new history merely to make an older plugin start.

For missing recent history, parent-directory scope or repeated sandbox errors,
see [retrieval troubleshooting](retrieval-troubleshooting.md). Updating a skill or
reader does not start ongoing Codex capture or automatically map older sources.

## Optional private coding sessions

Updating ordinary readers does not enable session privacy. The dedicated
[start/resume and owner-control workflow](private-sessions.md) is an explicit opt-in.
Desktop Chat/Cowork repository readers remain shared. Keep access leases, private
control records and suppression journals with operational stores; never delete them
to bypass a migration or maintenance refusal. No administrator permission or Full
Disk Access is required by Recall; same-user raw file access is outside its boundary.
