# Recall in Claude for Mac: chat and Cowork

For first installation or an upgrade, start with the [2.5 checklist](install-and-update.md).
It covers backup, schema migration, regenerated readers and loaded-client refresh.

Claude Code, Desktop chat and Cowork have different execution and connection
mechanisms. Installing the Code plugin does not configure all three. Version 2.5
includes a scoped app reader preparation command. Generated launch
configurations pass subprocess tests. Actual Desktop Chat and Mac-connected
Cowork discovery, cross-agent retrieval, same-session freshness and repository
isolation were exercised on the synthetic fixture in app 1.46388.4. These checks
do not certify automatic capture or human answer quality.

| Surface | Retrieval route | Capture of new conversations |
|---|---|---|
| Claude Code, including Code sessions launched by the app | Existing Recall plugin hooks/skill or scoped MCP | Hooks verified in Code; a particular app-launched session must actually load the plugin |
| Desktop **Chat** | Local stdio MCP entry in Desktop configuration | Not implemented; reading indexed Code/Codex/Cowork history does not capture the current chat |
| **Cowork with a connected Mac** | Observed `remote-devices` bridge to the local MCP reader; plugin carries the recall skill and a separate local connector | Selected local Claude-shaped JSONL files can be refreshed/watched with explicit host mapping; two real prefixes and appended records verified. New cloud-only tasks have no verified transcript source; no Cowork hook delivery claimed |
| **Standalone cloud Cowork, without a connected Mac reader** | Not provided by this host-local stdio package | Not implemented; no implicit account-history access or host-store upload |

Anthropic documents that plugins carry skills across chat and Cowork, while hooks
are unavailable in chat. [Plugin support](https://support.claude.com/en/articles/13837440-use-plugins-in-claude).
Anthropic's connector guide says Desktop local configuration is separate from
Cowork's remote connector mechanism. [Connector routing](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp).
**Observed behavior in app 1.46388.4:** a Cowork task with the “Claude Desktop
(macOS)” device badge used `mcp__remote-devices__recall-validation__...` tools and
successfully read the host store. Thus a categorical “Cowork cannot reach the
Desktop reader” claim is not justified for this version. The device bridge is
observed; the precise location of the model/agent loop was not independently
established from that badge or the task URL.

The local process still runs on the Mac. Cowork shell execution and cloud
sandboxes are separate environments; do not run a macOS Python path inside them.
The Mac must remain connected for its local reader to be useful. Access from a
standalone cloud task with no connected reader is not implemented or verified.
[Cowork architecture](https://support.claude.com/en/articles/14479288-claude-cowork-architecture-overview).
Sources checked September 6, 2026. Actual tool discovery takes precedence over
assuming capability from a surface name; retain the distinction between running
a local server and reaching it through a device bridge.

## Repository readers are not private sessions

Names such as `recall-project` and `recall-triton-msl` identify readers filtered
for different repositories in the shared store. A conversation with both readers
enabled can consult both projects. A refusal when the wrong reader receives a
block ID verifies that reader's configured filter; it does not make the project
private or prevent the same conversation from using its matching reader.

Installing these readers does not convert any session to private, move its
history or change capture policy. Session-private memory is a separate explicit
workflow described in [private sessions](private-sessions.md).

## Prepare a reader for one indexed repository

Run on the Mac, using an existing schema-compatible store and the actual project
directory, or an exact `--repo-id` from `recall_memory.py sources`:

```sh
python3 /path/to/recall/scripts/prepare_claude_app.py \
  --db /path/to/recall.db \
  --cwd /path/to/project \
  --output /path/to/new-recall-app-reader
```

Python 3.9+ with SQLite FTS5 is required. The preparer records the absolute Python
executable; `--python /absolute/path/to/python3` selects another installed runtime.
It checks that the store has sources in the selected scope, freezes that scope
into the launch arguments, copies the required stdlib reader modules, and emits:

- `desktop-config-snippet.json`, for Desktop chat;
- `recall-reader.zip`, with `.mcp.json`, a retrieval-only skill and the manifest
  at its root, for local Cowork;
- an unpacked `recall-reader/` used by the Desktop configuration;
- a `BUILD.json` with file hashes, selected repository and configuration details.

Desktop and plugin connectors have distinct names (`recall-reader` and
`recall-reader-plugin` by default), so tool traces can identify the route.
The output directory must be new. No history, credentials, hook scripts or capture
commands are bundled. This is a **machine-specific private configuration**, not
the public release ZIP: it contains local database/interpreter paths. Preparing it
does not install anything, migrate the store, start a watcher or restart an app.
The reader sees future commits from separately configured capture processes.

## Desktop chat setup

In Claude Desktop Settings > Developer > Edit Config, merge the generated entry
into `mcpServers` in `~/Library/Application Support/Claude/claude_desktop_config.json`.
Preserve existing servers and other settings; keep a backup before editing. Do not
overwrite an existing entry of the same name without reviewing it. Use `--name`
when preparing separate readers for separate repositories. Keep the unpacked
reader directory in its final location. Quit/restart only after running sessions
finish, then enable the reader for the chat and inspect its four tools.
[Official Desktop configuration guide](https://modelcontextprotocol.io/docs/develop/connect-local-servers).

The MCP tool descriptions support natural discovery without requiring a skill
installation. If a Recall skill is present, it chooses the MCP interface first.
Do not execute host paths in chat's code sandbox.

## Cowork setup with a connected Mac

Open Customize > Plugins and upload the generated `recall-reader.zip`. Enable its
MCP component and skill. This local plugin route is separately configured from the
chat entry. Use the plugin-named reader when available and record the actual
connection used. If local MCP is disabled or no device reader is exposed, report
that explicitly rather than creating an empty sandbox store. Do not substitute
`localhost` as a remote connector URL or expose the database via a public tunnel.

For an update, upload the newly generated ZIP under the same plugin name,
choose **Replace**, and retain its MCP component. After running tasks finish,
quit and reopen Claude before testing a fresh Mac-connected Cowork task. In app
1.46388.4, replacing an upload and starting a new task still used old loaded
reader processes until the app restarted. A schema mismatch after replacement
can therefore be stale code; do not repeatedly migrate the already-current store.

Use the preparer's ZIP directly. Its `.claude-plugin/plugin.json` and `.mcp.json`
are at the archive root. Zipping the outer output directory adds an invalid extra
folder. Validate/extract the actual upload ZIP and launch its reader during QA;
testing only the adjacent unpacked directory misses packaging mistakes.

Cowork also accepts marketplace repositories; the old README's “ZIP only” claim
was incorrect. A generic Code-plugin install still does not supply the explicit
scope/database connection configured by this preparer.
[Cowork installation](https://claude.com/docs/cowork/guide/plugins),
[plugin MCP format](https://code.claude.com/docs/en/plugins-reference#mcp-servers).

## Import selected local Cowork history

The observed local app version stores Claude-shaped transcripts under
`~/Library/Application Support/Claude/local-agent-mode-sessions/.../.claude/projects/.../*.jsonl`.
This path is an observed implementation detail, not a stable public app API.
`audit.jsonl` is not a conversation transcript. Select specific main-session
files; do not scan the entire app support directory or include subagents blindly.

The existing explicit `recall_memory.py index FILE --agent claude --cwd PROJECT`
can import an identified transcript. Verify `sources` afterwards: VM working
directories do not necessarily identify the corresponding Mac repository. Use
the explicit `rescope` command when the retained source needs a different scope.
For ongoing capture from a known local source, explicitly map its VM context to
the host repository:

```sh
python3 /path/to/recall/scripts/recall_capture.py --db /path/to/recall.db \
  --agent claude --path /path/to/selected-session.jsonl --cwd /path/to/project --watch
```

The mapping stays pinned across appends and later default imports. An existing
source in another repository is refused; inspect it and use explicit `rescope`
first. A selected main-transcript directory can discover new files under that same
mapping, so do not map a mixed-project directory to one repository. No watcher is
installed automatically. Real local prefixes plus new records were exercised in a
scratch store; that does not prove cloud-only tasks expose equivalent files.
Current cloud-only Cowork and Desktop Chat have no verified automatic transcript
source. They can read existing indexed history through the connected Mac. An
export adapter needs an authorized format sample and explicit branch/attachment
semantics before it can be advertised. Cowork compaction hooks are not verified. The app
reader deliberately bundles no Code capture hooks that could write an unrelated
VM store and falsely appear to share host memory.

## Acceptance checks, independently in chat and Cowork

1. Confirm the execution surface/mode and that all four Recall tools appear.
2. Ask naturally about a known indexed decision without naming Recall. Record
   whether the model searches, retrieves the original block and cites its offset.
   For quotations, call get with `quote` and the cited window (optionally the earlier
   `content_hash` as `expected_hash`); check `citation_check.valid` and exact returned
   quote offsets. Reject a fabricated quote and a quote attached to the wrong ID.
   Check that a requested patch is not reported as a verified resulting file state,
   and that compatible accounts from different agents are not called contradictory.
3. Verify the returned source agent and repository. Test another repository's
   known block ID: get must reject it. Check a false premise stays unconfirmed.
4. Append a synthetic decision through the separate capture command; ask again in
   the same reader session and confirm it sees the new evidence.
5. Keep capture separate: a new app message is not considered retained until an
   actual source import and later retrieval prove it. A plugin checkmark alone
   does not prove either capture or retrieval.

These checks validate connector behavior and capture boundaries. They do not
establish broad model answer quality or replace human review of retrieved evidence.

For conversations lacking an accessible local transcript, use the explicit
[visible-message snapshot or selected export import](history-import.md). This
requires deliberate capture and does not turn the read-only connector into a writer.
