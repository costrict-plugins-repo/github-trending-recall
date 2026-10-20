# Retained revisions and atomic rebuilds (P38/P52, schema 10)

A bare block ID reads published current text. A saved `(block_id, content_hash)`
selects exact retained redacted text with `get ID --revision HASH` (MCP: `revision`).
An expired, pruned or never-retained version fails explicitly. `expected_hash`
checks the returned version; it does not select a version. Quote verification
still uses Unicode character offsets in the selected text. Compaction excerpts
include the selected content hash in their get command; they do not automatically
pin revisions or override the retention policy.

A rebuild writes candidate blocks and byte-range hashes outside published
blocks/chunks/FTS/vectors. At EOF, explicit index revalidates every scanned byte
range and publishes the complete replacement in one transaction. Readers retain
one SQLite snapshot per operation. Edited and deleted published blocks are archived;
unchanged passages and vectors survive. Search and brief use only published current
blocks, never archived text or staged candidates. These atomic guarantees apply to
the durable block interface; legacy exchange commands keep their separate capture
behavior. Append capture continues to
publish completed capped passes. An ordinary append check still detects only
header/tail edits and shrinkage; it does not detect arbitrary middle-file edits.

Hooks never perform final rebuild publication: `rebuild_ready` means run the
explicit `index PATH --agent AGENT` command without `--rebuild`. Final publication
is maintenance, potentially longer than a hook timeout. Failed publication rolls
back; earlier published evidence remains. Restart a changed candidate with
`--rebuild`. Rescope waits for a pending rebuild to finish.

## Retention and portability

- Default: three superseded unpinned versions per logical block. This bounds version
  count, not total bytes. Large edited blocks and explicit pins consume more storage.
- `revisions ID` lists retained historical metadata. Current text may not appear in
  that list; use get for current text. Pinning current text materializes a snapshot.
- `pin-revision ID HASH` protects a retained version from ordinary cleanup.
  `--unpin` allows immediate expiry under the current limit.
- `revision-gc --keep N` sets the global nonnegative count and removes excess
  unpinned versions. Back up before decreasing it. Pins remain until unpinned/pruned.
- Source prune removes current, staged and archived rows. Backups include all of
  them. Export defaults to published current blocks; `--include-revisions` produces
  v2 with retained history/pins, excluding unpublished candidates.
- Import validates all identities/hashes before any write and applies the target
  redaction and retention policies. Re-redaction can change old hashes. Normal
  portable imports merge; history-import replaces the selected visible snapshot.

Schema-9 migration preserves available text but cannot recover already-overwritten
versions. An interrupted old mixed rebuild is marked changed and requires a fresh
rebuild. Historical text and metadata are immutable via the runtime contract and
SQLite update trigger; pins/retirement ordering can change. A recurring identical
text hash selects its first retained metadata snapshot. Source scope remains the
current explicitly assigned scope. This is local recovery, not an authenticated,
append-only audit log or a queryable archive of complete past session generations.
Doctor/restore validate archived identity/hash integrity and never invent evidence.

## Resource receipt

An unchanged rebuild of a copied 333,143,643-byte transcript retained 30,958 blocks.
163 staged passes took 6.78 seconds, maximum 83 ms; explicit publication took 0.83
seconds. Peak RSS was 39.4 MB. Database size rose from 95.2 to 139.2 MB while staging;
freed pages remain reusable after publication. This does not bound changed-history
storage or every possible publication. `benchmarks/revision_budget.py` reproduces
the measurement without touching the live store. Existing capture/compaction
budgets remain separate checks.

## Finishing a large rebuild

The CLI defaults to a ten-second overall index budget. A large rebuild may stop
in `rebuilding` or `rebuild_ready`. While that replacement is pending, hooks stage
new turns too: search and compaction recovery continue to see the earlier published
history, including none of those new turns. Give explicit maintenance enough time,
for example `index PATH --agent claude --rebuild --seconds 120`, or repeat
`index PATH --agent claude --seconds 120` without `--rebuild` until state is
`complete`. Repeating `--rebuild` starts over. The seconds budget is checked between
passes and is not a hard timeout on final publication.
