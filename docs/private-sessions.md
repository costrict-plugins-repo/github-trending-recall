# Private Recall sessions

This opt-in workflow gives one root Claude Code or Codex session its own Recall
store and reader. Other supported Recall readers cannot retrieve its blocks,
revisions, neighbors or search statistics. Shared Desktop Chat/Cowork connectors
remain repository readers; they do not provide conversation-specific privacy.

**This is not an operating-system sandbox.** Agents with arbitrary access to the
same OS account can read raw files. Recall does not request administrator access
or Full Disk Access. Stronger protection requires a host sandbox or separate OS
identity that actually denies access to the other transcripts and stores. No such
protection is claimed here. Native transcripts, independently configured capture pipelines/stores, backups, exports, shared excerpts
and passages already given to a model remain outside Recall's revocation boundary.

## Start and resume

Use Python 3.9+ on a POSIX host. The native controller has been exercised on macOS
with Claude Code 2.1.263 and the Codex 0.153.4 binary bundled with the desktop app.
Native Codex uses the app-server API, whose dynamic-tool fields are experimental.
A host upgrade that changes that contract requires another compatibility check.
The controller does not log in for you or change your normal app configuration.
Its child environment points ordinary Recall hooks at the selected shared suppression
store. Existing host security hooks and permissions remain in force; custom capture
hooks with their own explicit database or export destinations require separate review.
Use an authenticated native executable. No private session is started by installing
or updating the ordinary plugin.

Run from a terminal, replacing `/path/to/recall` and `/path/to/project`. The prompt
is read from stdin; the native host handles coding with its ordinary sandbox/tool
permissions. Each invocation runs one turn. Continue with the explicit profile;
there is no implicit “most recent session,” fork, or subagent grant.

```sh
python3 /path/to/recall/scripts/recall_private_session.py start \
  --agent claude --binary /opt/homebrew/bin/claude \
  --cwd /path/to/project \
  --shared-db "$HOME/.claude/context-recall/recall.db" \
  --directory "$HOME/.claude/context-recall/private" \
  --profile "$HOME/.claude/context-recall/private/project-session.json" <<'PROMPT'
Inspect this project and explain the failing test before changing it.
PROMPT
```

For Codex, use `--agent codex` and the exact current executable, such as
`/Applications/ChatGPT.app/Contents/Resources/codex`. A stale Homebrew `codex`
binary may not have the required app-server interface. `--model` is an optional
native override; omitting it preserves the host default.

```sh
python3 /path/to/recall/scripts/recall_private_session.py resume \
  --binary /path/to/the/same/native/executable \
  --profile "$HOME/.claude/context-recall/private/project-session.json" <<'PROMPT'
Continue the work using the decision from the previous turn.
PROMPT
```

Use the `compact` subcommand with the same `--profile` and `--binary` to request
native compaction without changing the owner. It does not delete retained Recall
evidence.

A profile is created once with owner-only permissions. Concurrent reuse is
refused. Codex checks the host's task ID on every dynamic tool call and rejects
fork/subagent IDs. Claude uses a fixed session ID with native print/resume and
exposes Bash, Read, Edit, Write, Glob and Grep; Agent/Task delegation is disabled.
The launcher offers no arbitrary native flag passthrough, `--fork-session`,
“continue latest,” permission bypass, or model-accessible maintenance tool.
Codex commands requiring additional permissions are refused by this runner;
there is no automatic elevation. Use a workspace where the intended operation
is permitted. This deliberately smaller interface does not reproduce every
interactive native CLI feature.

Capture runs at Claude lifecycle hooks and at Codex turn/tool boundaries, not in
an always-running daemon. Only the exact bound transcript is eligible. Work
started outside the private controller has no private reader grant. If private
storage, routing, identity, or control metadata is unavailable, access/capture
fails closed; it never falls back to the shared store. Interrupted/large rebuilds
remain staged until explicitly published by owner maintenance.

## Owner controls

The profile records the exact `owner`, `cwd`, shared database and private path.
Use its values for maintenance. The following common prefix is written out here
for clarity; these commands belong in your terminal, not a model's tool catalog:

```sh
python3 /path/to/recall/scripts/recall_private.py \
  --shared-db /path/to/recall.db --directory /path/to/private \
  --owner codex:EXACT-NATIVE-UUID --cwd /path/to/project status
```

Substitute one of these commands after the common arguments:

| Command | Effect |
|---|---|
| `index --transcript /exact/native.jsonl --seconds 30` | Finishes backlog or publishes a staged rebuild for the exact owner. Use `--rebuild` to explicitly rebuild a changed source. Repeat or increase the bounded budget if necessary; paused/deleted capture remains refused. |
| `pause` | Stops private capture; existing evidence remains readable. |
| `resume-capture --allow-backfill` | Resumes capture with explicit acknowledgment that the disabled interval can be imported from the native transcript. |
| `revoke` | Disables capture and invalidates existing reader connections. |
| `grant` | Allows a newly started owner connection. Old connections stay revoked; capture stays off until explicitly resumed. |
| `preview-delete` | Counts the exact retained material and returns a review fingerprint. |
| `delete --reviewed FINGERPRINT` | Revokes first, then purges and cleans private content. Interrupted deletion stays inaccessible and can be retried. The owner remains permanently suppressed; use a new native session to start over. |
| `backup --output /path/to/new-backup.db` | Creates an owner-only, owner-tagged SQLite backup without an access grant; refuses overwrite. |
| `preview-restore --input /path/to/backup.db` | Validates exact owner, repository, schema, FTS and integrity before touching the target; returns a fingerprint. |
| `restore --input /path/to/backup.db --reviewed FINGERPRINT` | Restores validated private content and revokes old connections. It leaves capture off. Explicitly grant/resume after inspection. A deleted owner cannot be restored. |

Controls drain cooperating Recall connections; a busy store is refused before
maintenance changes it. Retry after the connection closes. No force-unlock option
is provided. Deleted stores retain an empty owner-tagged database and control
metadata so they cannot accidentally become public or be silently recreated.
Deleting that metadata is not a supported reset.

## Convert existing shared history

`start` creates a new native session. To move an already indexed root source,
stop obsolete clients, back up, and use `preview-conversion`, then
`convert --reviewed FINGERPRINT` with the same owner, repository and directory.
Changed content invalidates the preview. `recover-conversion` resumes an
interrupted recorded transition; it cannot select another owner or destination.
See [the conversion contract](privacy-conversion.md) for copied tables and limits.
The maintenance conversion alone does not attach an existing desktop task to a
private reader. Do not claim an app task is private simply because conversion
completed. The dedicated runner's start/resume profile is the supported attachment.

## Share only reviewed excerpts

Write a JSON selection file containing exact current block IDs and character ranges:

```json
[{"block_id":"EXACT_BLOCK_ID","start":0,"end":120}]
```

Run `preview-share --selection /path/to/selection.json`. Read the returned text,
roles, timestamps and fingerprint, then run
`share --selection /path/to/selection.json --reviewed FINGERPRINT`.
Only those excerpts become a separate `shared-snapshot:` source in the same
repository. Neighbors, hidden revisions and the rest of the session are excluded.
The private capture route never changes. Sharing is bounded to 100 ranges and
1 MiB of selected text per operation. A changed block or selection requires a new
preview. Repeating an identical approved disclosure is idempotent.

The shared snapshot is a disclosure. Deleting the private source does not retract
it. The shared store's ordinary explicit `prune shared-snapshot:...` can remove
that retained snapshot, but cannot retract other models' context or other copies.

## Backups, updates and rollback

Keep `.access.lock`, `.private-control.json`, session profiles, the shared
capture-policy journal, and any transition receipts/gates with the operational
store. Readers create none of these. A content backup contains no new access
grant. Restoring private content never restores an old reader epoch. A restored
shared backup is filtered against current private routing decisions.

Do not downgrade a policy-bearing/private deployment to schema-10 software.
Those writers do not understand the privacy protocol. Keep the current compatible
runtime for recovery, or remain stopped. A pre-privacy backup contains previously
shared history; restoring it with an old runtime would disclose that history again.
A binary downgrade is not a privacy-preserving rollback. Use the verified current
restore workflow and keep suppression metadata intact.

The native-host fixture exercises start, read tools, a shell command and resume.
Its provider responses are scripted and do not establish model answer quality.
Fresh-model results, authentication limitations and resource measurements belong
in the accompanying validation receipt. The practical human recovery review is accepted; session privacy
has a separate enforcement test matrix.
