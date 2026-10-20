# Session privacy: implementation scope and validation

Released September 7, 2026 with Recall 2.5.0, schema 12. The dedicated
Claude Code and Codex controllers implement native attachment, capture,
retrieval and reviewed transition controls. Existing sessions remain shared
unless explicitly converted. See [supported hosts and validation boundaries](session-privacy-hosts.md)
and [the private-session workflow](private-sessions.md).

## What users should be able to choose

| Mode | New Recall capture | Who can retrieve it through Recall |
|---|---|---|
| Shared | Retained in the selected shared scope | Readers authorized for that scope |
| Session-only | Retained in a separate private store | A reader bound to the owning session; no other session by default |
| Do not retain | No new conversational content retained by Recall | No newly captured content; handling existing memory is a separate explicit choice |

Session-only describes outgoing visibility. The owner may still read explicitly
authorized shared history. Subagents, forks, resumed tasks with a different host
identity, and other agents do not inherit private access automatically. Resuming
the same verified identity may retain access. Any deliberate additional grant is
an explicit user action, never something inferred from a transcript or task name.

New setup should explain these choices before capture starts. Existing users keep
their existing shared behavior on upgrade, with a clear notice and an explicit
conversion path. Do not silently declare existing history private. A configured
private default with missing identity or policy must skip capture and refuse
private reads, rather than fall back to the shared store.

## Permissions and the actual security boundary

**Users do not need to give Recall administrator access, Full Disk Access, or
broader filesystem privileges to choose a memory-sharing mode.** Recall needs
only access to the selected transcripts, stores and configuration required for
the chosen workflow. A host may ask the user to approve a narrowly scoped local
reader or those paths during setup. Those approvals are not what creates privacy.

There are two distinct guarantees:

1. **Recall access isolation:** another conversation cannot retrieve a private
   source through its configured Recall reader, including by guessing block IDs.
   This requires trusted reader binding and complete enforcement in Recall.
2. **Host-enforced isolation:** another agent also cannot bypass Recall and read
   the SQLite files, WAL, backups, original transcripts, credentials or policy
   files. This requires restricting that agent's filesystem/process access with
   a verified sandbox or separate security identity. Broader permissions weaken
   this boundary. Owner-only file permissions do not separate processes running
   as the same OS user. Encryption with a key accessible to both does not fix it.

The official [MCP security policy](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/SECURITY.md)
states that a stdio client and its subprocess run with equivalent environment
privileges; stdio alone is not a defense against a malicious client. This design
therefore treats a model-visible session ID, environment variable, CLI argument,
repository path or secret stored in an agent-readable file as insufficient to
prove an independently protected caller.

The product must show which guarantee is established. Proposed wording:

> Session-only memory is available only through this session's Recall reader.
> Other processes with access to your memory files or original transcripts may
> still read them. Preventing that requires host access restrictions. Recall has
> not verified those restrictions on this setup.

Show a stronger host-isolation status only after an actual negative access test.
If the host cannot bind a reader to one conversation, report **Session-only
unavailable on this connection**. A shared app connector is not a private-session
connector simply because it has a different display name.

## Gaps that motivated the design (installed shared runtime)

- `recall_mcp.py` fixes `repo_id` at launch and checks it for source filters and
  direct current/revision reads. It has no authenticated conversation principal.
  Its optional `source` argument is a query filter.
- `recall_memory.py` offers `--all`, direct ID reads, export and maintenance;
  it currently assumes a trusted local operator. It cannot remain an unrestricted
  agent-accessible bypass while claiming session access enforcement.
- `settings.py` has no per-source privacy policy. Capture is also reached through
  hooks, explicit indexing, Codex import, the watcher and history import.
- Legacy exchanges, highlights, connections and compaction fallback are separate
  paths. Protecting durable FTS alone would leave disclosures through these paths.
- Current app readers are configured for repositories and can be available to
  multiple conversations. Existing discovery receipts prove repository access,
  not conversation-bound authentication or host sandbox isolation.

## Recommended implementation

### 1. Policy and identity before content

Introduce an agent-namespaced source identity and a versioned local policy
registry. Policy records contain only the minimum identity/routing information,
not prompts or query text. Keep a suppression record for do-not-retain sources
so directory scans, rebuilds and reimports cannot silently capture them again.
Stable import identities and duplicate-path handling must be part of this rule.

Resolve policy before any durable or legacy content write, automatic tagging,
highlighting or embedding. Unknown, corrupt or unsupported policy state must
not publish potentially private content. User management is a separate trusted
control path; retrieval tools cannot change privacy or grant themselves access.

Bind each private reader to an owner identity outside model-controlled tool
arguments. Test whether each host can establish that binding at all. Hook-provided
session metadata can aid normal routing but does not alone prove protection from
an agent that can forge local commands. Do not invent a trustworthy identity from
MCP initialization metadata or `RECALL_SESSION_ID`.

### 2. Separate private storage and restricted readers

Keep session-only content in a per-owner store, outside shared capture roots.
This also keeps its FTS statistics, vectors and metadata out of other sessions'
ranking and coverage. A policy flag in a shared database is insufficient for
older clients, unrestricted SQL readers and private-corpus ranking effects.

A restricted reader may search its authorized shared store and its own private
store. It must resolve current blocks, old revisions and neighbors only within
those stores. Cross-store ordering, pagination, identity collisions and citations
need deterministic handling. No process or embedding model should be started for
every historical private session; open only authorized stores needed by a request.

The agent-facing CLI needs the same restricted service contract. Keep owner
maintenance commands clearly separate. On a host-isolated setup the agent must
not be able to launch the unrestricted maintenance CLI, mutate policy, read other
stores, or inspect another session's launch credentials. If the host cannot
enforce this separation, describe the protection as Recall access isolation only.

### 3. Cover all reads and writes

Use one access/routing layer for durable and legacy capture and retrieval. Audit
search, semantic candidates/ranking, get/revision/quote, neighbors, brief, recovery,
last-N exchanges, session listings, source counts, suggestions, highlights, tags,
connections, export and diagnostics. Denied and nonexistent IDs should not reveal
which private sources exist. Privacy administration can show more detail to the
trusted owner; ordinary reader status must show authorized coverage only.

Import/export must preserve restrictive policy or require explicit owner-selected
sharing at the destination. Untrusted imported metadata cannot grant access or
relax an existing policy. Reimport, rescope, fork discovery, revision pinning and
garbage collection must not change visibility. Backup and restore preserve policy
and routing; a stale backup cannot silently resurrect an owner's later suppression
decision. Whole-store backup is an owner operation, not a session reader tool.

### 4. Existing data, changing modes and failures

Turning capture off does not delete earlier records. Offer a separate reviewable
action to remove existing Recall memory. Enumerate durable blocks, staged rebuilds,
revisions/pins, vectors, legacy exchanges and derived highlights. Removing content
must not remove its capture-suppression policy. Original host transcripts and
previously exported files remain outside this deletion operation.

Shared-to-private conversion needs an exclusive maintenance workflow: deny shared
reads for the selected source, stop/drain relevant writers/readers, copy and verify
private data, remove all active shared representations, then activate the private
reader. SQLite transactions cannot atomically move data across independent stores
in WAL mode; use a recoverable journal/state machine with denied intermediate
states and fault-injection tests. Inspect sidecars and old shared database files
before declaring conversion complete. Do not promise forensic secure erasure.

Already returned passages, model context, copied answers, provider history, prior
backups and raw transcripts cannot be revoked by a policy change. A private session
can also disclose text by deliberately sending or pasting it elsewhere; this is
not automatic information-flow tracking. Explain these limits at conversion.

Private-to-shared is an explicit disclosure action with a scope/content preview.
Revocation applies to new requests after acknowledged completion; an in-flight
request must finish or be cancelled before acknowledgement. A resumed capture
transaction must not republish a source whose policy changed meanwhile.

Changing these contracts requires a new store/policy compatibility boundary.
Old binaries and already-running readers must fail closed or be stopped before
private data is introduced. A schema bump alone is not proof: test actual old
readers, hooks and maintenance commands. Rollback must preserve suppression and
must never route private stores into an older shared-only client.

## Host readiness gates

| Surface | What must be established before session-only is offered |
|---|---|
| Claude Code | Exact hook source routing, a reader bound to that session, no shared legacy fallback; separately test filesystem/CLI bypass restrictions if claiming host isolation |
| Codex | Trusted task identity and per-task reader attachment; validate resume, fork and worktree behavior without trusting model-provided scope arguments |
| Claude Desktop Chat | Verify a connection can be restricted to the intended conversation; current app-wide repository readers do not establish this |
| Mac-connected Cowork | Independently verify task-specific connector/reader access through both available routes, plus VM/Mac filesystem boundaries |

These are implementation acceptance gates, not verified host capabilities. When a
host cannot provide them, retain shared retrieval or do-not-retain behavior with
clear limits; do not substitute a global “private” connector accessible everywhere.
Any OS sandbox/broker component must be separately justified and tested before
shipping. Host feasibility must be established before promising session-only availability
on an additional surface.

## Regression coverage contract

All following rows are automated where possible. Human permission dialogs and
the accuracy/usefulness judgment remain distinct from enforcement tests.

| Area | Required evidence after implementation | Release interpretation |
|---|---|---|
| Access matrix | Owner, same-repo foreign session, foreign repo, no identity, forged identity and subagent/fork against shared/private/off sources; fresh random synthetic secrets | Scoped shared readers do not grant private access |
| Every read route | Deny guessed current/revision IDs, neighbors, lexical/semantic snippets, legacy reads, brief, compaction fallback, highlights, exports and existence/count leaks | Current and retained evidence use the same owner boundary |
| Every capture route | Both adapters, hooks, watch, bulk/recursive index, imported snapshots, new and appended records; suppression survives path aliases, rebuild and reimport | Suppression applies across supported adapters and import paths |
| Concurrency and transitions | Racing capture/rebuild/read with mode changes, pinned revisions, revocation, crashes at every move step, interrupted cleanup, restart/recovery | Exclusive leases and resumable transitions protect cooperating clients |
| Upgrade and rollback | Empty install and schema 5/9/10 upgrade, old live reader rejection, backup/restore, policy preservation and rollback refusal | Current restore preserves restrictive policy; obsolete readers are refused |
| Real host flows | Fresh and existing Claude Code/Codex sessions; Chat and Cowork routes; owner succeeds, another conversation fails, post-compaction/restart still enforced | Native coding controllers are supported; general Chat/Cowork private readers are not |
| Host isolation claim | From an unauthorized agent, attempt raw DB/WAL/transcript/backup reads, credential/config access, unrestricted CLI and policy mutation | Same-account raw filesystem isolation is explicitly outside this release |
| Cost and correctness | Full suite; hook/capture/compaction budgets, mixed-store search latency, memory/storage and token-output comparison; optional vectors remain off by default | Fixture budgets and context measurements are distinct from universal guarantees |

The 50-case practical human recovery review was accepted. Further evaluations
should continue checking answer accuracy. Existing clean shared
retrieval observations remain evidence for their original configuration; they
do not certify privacy. Mark answer-key-exposed runs separately (including the
reported round2-02 answer). A fresh conversation alone does not remove answers
already present in its authorized corpus. Use a separate frozen source corpus
without evaluation reports for clean retrieval checks, and ensure test chats are
not automatically imported into it. When the access layer changes, rerun shared retrieval
regressions and new owner/foreign-session cases. Repeat affected model-quality
cases if ranking or context changes; do not require the maintainer to relabel all
50 merely because an access layer was added.

## Release validation

The release includes owner-bound native attachment, shared/private capture
routing, enforced reader checks and reviewed conversion, deletion, sharing and
backup/restore. Validation covers the access matrix, native lifecycle, migration
preservation, first install, upgrades and resource budgets. The release suite
passes 900 tests, including three packaging checks; the packaged runtime has
897 packaged runtime checks. Small fresh model checks and practical
human acceptance remain distinct from general answer-quality claims.

Opting into private memory requires the documented dedicated workflow. It does
not create OS isolation from other processes running as the same account.
