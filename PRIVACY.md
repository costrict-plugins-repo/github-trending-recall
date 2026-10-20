# Privacy Policy — Claude Recall Plugin

**Last updated:** September 7, 2026

## What Data Is Stored

The recall plugin stores conversation data locally on your machine to enable context recovery across sessions. Specifically:

| Data | Location | Purpose |
|---|---|---|
| Exchange text | `~/.claude/context-recall/recall.db` | User prompts (up to 1,000 chars) and assistant responses (all text blocks of the turn merged, up to 4,000 chars) for search and recall |
| Durable blocks and passages (2.5+) | Same DB (memory tables) | Complete redacted user/assistant text and tool-call inputs, Claude compaction summaries labeled `host`, plus derived search slices. Unlike legacy exchange rows, retained blocks are not truncated |
| Tool calls (v2.4+) | Same DB (exchanges.tool_text) | One line per tool call Claude made in the turn: the requested shell command, file path, URL, or tool name; these record intent, not successful execution (300 chars per call, 2,000 per exchange). Tool **output** is never stored |
| Session metadata | Same DB | Session IDs, project paths, timestamps, byte offsets for incremental indexing |
| Auto-tags | Same DB | Technical terms extracted from exchange text for search and discovery |
| Manual tags | Same DB | User-applied tags for organizing sessions and exchanges |
| Highlights | Same DB | Summaries of findings flagged for cross-session sharing |
| Connections | Same DB | Opt-in links between sessions for highlight sharing |
| Session config | Same DB (sessions.metadata) | User preferences (skill_enabled, check_mode, etc.) |
| Usage counter (v2.2.3+) | Same DB (invocations) | Timestamp, session ID, project hash, command name, and command arguments for legacy CLI and maintenance invocations — arguments may include legacy search terms you typed. Durable read-only CLI/MCP retrieval does not add counter rows. Powers `/recall usage`; never leaves your machine |
| Recall events | `~/.claude/recall-events.log` | Timestamps and session IDs when `/recall` is invoked (for observability) |
| Settings (2.5+) | `~/.claude/context-recall/settings.json` | Explicit opt-ins only (Codex import on/off, its directory and time budget). No conversation content |
| Backups (2.5+, on request) | Wherever you point `backup DEST` (the default location suggested by docs is the same directory) | A complete copy of the store, including retained text. Delete backups when you delete the store |

## Where Data Is Stored

All data is stored **locally on your machine**, by default under `~/.claude/context-recall/`; backups, exports and a store selected with `RECALL_DB` live wherever you put them. The plugin:

- Does **not** transmit data to any external server
- Does **not** make any network requests
- Does **not** independently send stored data to third parties. Recalled passages enter the host agent's context and may be sent to its configured provider
- Does **not** send telemetry or analytics, or track users. Optional local operational diagnostics are described below

The default database directory is created with owner-only permissions (0o700) when the plugin first creates it; an existing directory, or a store you selected elsewhere, keeps the permissions it has.

### Optional local diagnostics

MCP readers and the independent capture command accept an explicit `--diagnostics /path/to/events.jsonl` flag. It is off by default, with no environment-variable fallback and no upload endpoint. The parent directory must already exist. Logging stores only UTC event time, a fixed operation/outcome category, elapsed milliseconds, and numeric result/output/capture counts. It does not store queries, recalled text, commands, paths, block/session/repository identifiers, provider credentials or raw exception messages. It does not measure model billing or answer quality.

The logger creates owner-only files and retains at most two 1 MiB segments plus an empty lock file. It refuses unrelated, symlinked, hardlinked or non-private destination files. Lock contention, write failure or unsupported POSIX locking drops events and emits one content-free stderr warning per process; retrieval/capture continues. Slow disk I/O is not a hard latency bound. Diagnostics do not instrument legacy commands, Code hooks or the model provider; those retain their existing local status/error behavior. See [diagnostics](docs/diagnostics.md) for enabling, reading and disabling logs.

## What Data Is NOT Stored

- Complete raw transcripts: durable adapters exclude system/developer records, reasoning, tool results, image content blocks, and inline base64 data URIs. User/assistant prose and tool-call inputs are retained in full after redaction
- Credentials that match the redaction patterns below (see Secrets Redaction; this is pattern-based, not a guarantee)
- System information beyond project directory paths
- Other application histories unless explicitly selected for import (Codex JSONL imports are supported)

Claude `isMeta` skill/command bodies are excluded from new durable capture. Claude compaction summaries remain searchable as role `host`, but brief and compaction recovery do not select them as conversational evidence. Codex compaction summaries are excluded. These changes apply to newly indexed records; explicitly rebuild an existing source to reclassify old records and remove previously retained metadata after the rebuild finishes. Legacy exchanges still retain capped host-rendered prompts; this pre-existing limitation is separate from the durable adapter.

## Secrets Redaction

Newly captured text (prompts, replies, tool-call lines and durable blocks) passes through `redact_secrets()` before it is written, replacing matches with `[REDACTED:<kind>]`. Text that enters the store by other routes keeps whatever redaction it had when it was produced: a v1 index migration, `import-export`, `restore` of an older backup, and highlights or tags you write yourself. The patterns:

- Well-known token formats: AWS access keys, OpenAI and Anthropic keys, GitHub tokens, Hugging Face tokens, Slack tokens, Google API keys, JWTs, `Bearer …` tokens, PEM private-key blocks
- Assignments whose name looks like a secret (`API_KEY=…`, `"password": "…"`, `AWS_SECRET_ACCESS_KEY=…`)
- Plain-language phrasings such as "my password is …" or "the api key is …"

This is pattern matching, not detection of every secret. Anything that does not match one of these shapes is stored as typed. If you paste something sensitive that slips through, `prune --session <id>` removes that session; the redaction list lives in `scripts/utils.py` and pull requests adding patterns are welcome.

## Data Retention

Current retained history persists until you explicitly delete it; sessions are not automatically pruned. Superseded unpinned revisions are bounded separately (three per block by default), as described below. You control deletion and revision retention:

- `/recall prune --session <id>` — delete a legacy session and matching durable data
- `python3 scripts/recall_memory.py prune AGENT:SESSION` — delete an explicitly imported durable source, passages and vectors; legacy exchange rows and original transcripts remain
- `/recall prune --before <date>` — delete all sessions before a date
- `rm -rf ~/.claude/context-recall/` — delete all recall data in the default location permanently (backups, exports and alternate stores you created elsewhere are separate files)
- `rm ~/.claude/recall-events.log` — delete the event log

Versions prior to 2.0 stored snapshots as JSON files (`*_index.json`, `current.json`, `recall-config.json`) in the same `~/.claude/context-recall/` directory. Current versions no longer write these, but old files may remain; the `rm -rf` above removes them along with everything else.

## User Control

You have full control over what the plugin stores:

- **Opt-in features**: auto-highlight detection, decay polling, proactive recall suggestions and the optional recall-assistant skill are disabled by default; you enable them via `/recall config`. **On by default**: capture, the model-invoked `recall` skill, and compaction recovery (the verbatim excerpts of this session's own retained blocks described below).
- **Cross-session retrieval**: Explicit connections control automatic highlight sharing. Search and brief can retrieve other indexed sessions in the same repository, or all repositories with `--all`; they do not require a connection.
- **Deletion**: All data can be deleted at any time via the prune commands or by removing the database file.

## Third-Party Dependencies

Default capture, lexical retrieval and diagnostics use only Python standard library modules. Optional semantic indexing/search uses sentence-transformers and its dependencies, including NumPy and PyTorch, which the user installs separately. Recall neither installs these packages nor downloads models; semantic commands load a supplied local model with remote code disabled.

## Changes to This Policy

Changes to this privacy policy will be documented in the plugin's CHANGELOG.md and README.md.

## Contact

For questions about data handling: [https://github.com/bledden/claude-recall-plugin/issues](https://github.com/bledden/claude-recall-plugin/issues)

## Durable blocks in 2.5.0

In addition to the capped legacy exchange rows described above, the durable store retains complete redacted user/assistant text blocks and complete redacted tool-call inputs, with source agent/session/message identity, source path, Git repository identity, timestamps, and character/byte ranges. Search passages are derived slices; display limits do not impose a storage truncation limit. This increases local data retention and database size. Pattern-based secret redaction remains incomplete. System/developer records, reasoning blocks, tool results, image blocks, and inline base64 data URIs are excluded by the new adapters.

Claude hooks capture these blocks. Codex and bulk history imports run only against explicitly selected local files/directories, or, after you run `config codex_import on`, against your Codex sessions directory at each Claude session start (newest files first, a few seconds per start). The independent `recall_capture.py` command is another explicit opt-in: it creates/migrates the selected store and imports only the Claude/Codex files or directories named with `--path`; `--watch` repeats in the foreground until stopped. It does not install a service. Stop a watcher before pruning if later source updates should not be reimported. Recall does not implicitly import unrelated application histories. Retained blocks remain after original transcripts disappear. Explicit rebuild replaces a source; durable prune removes its blocks, search passages and vectors. Original transcripts and separate legacy rows are unaffected by durable-only prune. Legacy session prune removes both legacy and matching durable data.

After a context compaction, the session-start hook injects short verbatim excerpts of this session's own retained blocks (the opening ask and the tails of the last three text blocks, at most 3,500 characters) into Claude's context so it can re-anchor; like any recalled passage, that text then reaches the host agent's provider.

Deletion at a glance: legacy session prune removes that session's legacy rows and durable blocks; durable `prune AGENT:SESSION` removes blocks, passages and vectors but leaves legacy rows and the original transcript; `export`/`import-export` move complete redacted blocks; `backup`/`restore` copy the whole store.

Optional semantic indexing stores local embedding vectors plus the supplied model path/fingerprint. It loads an existing local model through an optional third-party sentence-transformers dependency, with downloads and remote code disabled. The default capture and lexical retrieval paths do not load it. Retrieved passages enter the host agent's context and may be sent to that agent's configured provider; local plugin storage is not a promise that a cloud agent never receives recalled content.


## Optional local MCP access

The optional `prepare_claude_app.py` command generates a private, machine-specific
Desktop chat configuration and local Cowork reader plugin. It bundles reader code
and absolute paths, not conversation history or credentials. Nothing is uploaded
or installed by preparation. The configured reader can return indexed evidence to
Claude; this does not capture new chat/Cowork conversations. Cowork may reach the
host-local server through the app’s remote-devices bridge while the Mac is online.
This does not install a server inside a cloud sandbox or expose a public endpoint. Do not distribute the generated private package as a
public release artifact. See `docs/claude-app.md` for the separate surface limits.

`recall_mcp.py` exposes four retrieval tools over stdio. The client chooses an explicit repository scope at launch, which applies to search, get-by-ID, brief, sources and derived counts. The server opens the existing store read-only and neither creates/migrates it nor captures transcripts, logs Recall invocations, repairs data or executes recalled commands. SQLite may use its normal WAL coordination sidecars. Capture remains a separate explicit process.

This interface opens no network listener. Evidence returned to an MCP client may be sent to that client’s model provider, just like evidence retrieved by the skill/CLI. It does not make the calling host local-only. Uninstall the separately configured MCP entry and stop the foreground watcher to disable those integrations; the retained store and original transcripts are separate data to delete.

### Host-record cleanup and citation checks

New legacy capture skips Claude records flagged `isMeta` or `isCompactSummary`.
Older legacy rows are not silently rewritten. `clean-legacy-host SESSION` audits
exact timestamp/redacted-text matches against that session's registered original
transcript; `--apply` clears only unambiguous matching prompt text and its preview,
and updates FTS. Exchange identities, replies, commands, tags and other annotations
remain. Original files and backups retain their contents. Derived tags can still
reflect old text; this targeted cleanup is not a purge of every historical effect.

Exact-quote/hash checks use the existing read-only get path. They add no persistent
quotation log or network call. As with all reader results, quotes sent through a
host agent may enter that host's model context. `recall_capture.py --cwd PROJECT`
explicitly maps selected local transcripts to a host repository and pins the
scope; it refuses to move an already registered foreign source implicitly.

## Superseded text and explicit history imports

Schema 10 retains up to three superseded unpinned redacted text versions per block.
Pins explicitly retain additional versions; `revision-gc --keep N` changes the
limit and removes excess unpinned rows. Source prune cascades through current,
staged and historical rows. Backups include history; portable export includes it
only with `--include-revisions`. SQLite deletion does not erase originals, existing
backups, free pages or storage remnants. No automatic vacuum or secure-erasure claim.

History preview/import reads only the explicitly supplied local file. ZIP handling
reads bounded conversation JSON members without extracting any files. Preview
prints conversation titles/IDs and counts, not message bodies. Import requires one
conversation ID and repository directory, retains supported visible text and applies
the normal redaction policy. New redaction rules can change imported hashes.
Snapshots do not prove authorship or completeness, and no account request, network
fetch, upload, credential access, background capture or telemetry is added.

## Opt-in private coding sessions (schema 12)

The [dedicated native workflow](docs/private-sessions.md) separates supported Recall
readers by root session and keeps private content out of shared indexes and counts.
It does not separate arbitrary processes running as the same OS user. It requires
no administrator access or Full Disk Access. Shared Desktop Chat/Cowork connectors
are not conversation-bound private readers.

Owner-only profiles and control records store source identities, repository paths,
capture/access state and a revocable connection generation. They are local files,
not telemetry. No permanent server, network listener, credential store or remote
history synchronization is added. Native clients still contact their configured
model provider when the user runs a coding turn. Exact excerpts are shared only
after a reviewed fingerprint; that disclosure does not enable general shared capture.

Revocation affects subsequent supported reads. It cannot retract text already in
a model context, native transcripts, other session copies, exports, backups or
filesystem snapshots. Deletion keeps suppression and an empty tagged store; it
does not promise forensic erasure. Private restore validates owner/scope/schema
and leaves access revoked and capture off. Do not downgrade active privacy to
schema-10 writers, which do not implement this control protocol.
