# Existing-history conversion contract

September 7, 2026. **Recall 2.5.0, schema 12. Conversion remains explicitly opt-in.** The
`recall_conversion` module is exposed through the owner-only `recall_private.py`
preview/convert/recover commands, never through model tools. Dedicated new-session
attachment and controls are described in [the user workflow](private-sessions.md).

## Reviewed scope

`preview` accepts one exact root Claude/Codex source and its existing repository.
It returns row counts and a fingerprint covering current blocks, chunks, vectors,
retained revisions/pins, staged rebuild blocks/segments, source metadata, revision
retention settings and optional vector configuration. Claude sources also include
legacy exchanges, tags, highlights, self-connections and session-linked invocation
rows. A missing durable source must be indexed before conversion is considered.

Cross-session connections involving the owner are counted and fingerprinted for
removal; they are not copied as grants into the private store. Existing descendant
sources are listed as preserved, separate histories. Copies in those sources or
other conversations are not retroactively revoked. A changed preview, including
changed links or settings, requires another review. No conversion is performed
by previewing, and no conversation text is printed in the preview.

## Transition and recovery

1. Acquire the shared store's exclusive cooperative connection lease. Any active
   updated reader, writer, legacy CLI or MCP connection causes a refusal before a
   transition is written. Existing host tasks are not interrupted automatically.
2. Recheck the reviewed fingerprint, then atomically write and sync a bounded
   owner-only transition journal. All updated public access stays closed while
   this marker exists, even after a crash releases the process's lock.
3. Suppress new shared capture. Construct a complete, empty, owner-tagged private
   store before publishing its filename. Copy only the selected rows into it,
   using a private transaction and a separate exclusive lease. Regenerate its
   FTS indexes and verify hashes, settings, schema, foreign keys and FTS integrity.
4. Persist the private routing decision outside the restorable content database,
   then remove the owner's shared representations in a transaction. Preserve
   unrelated sources, including another adapter's identical bare session ID.
5. Rebuild both shared FTS indexes, vacuum the shared DB and truncate its WAL.
   Verify the private copy again, write a per-owner completion receipt and clear
   the transition gate. Access resumes only after the exclusive leases close.

`recover` reads the recorded owner, target, repository and fingerprint; it never
invents a replacement destination or falls back to shared capture. The copy and
cleanup are idempotent. A partial copy rolls back; a committed verified copy can
be reused. A changed/damaged private copy, missing committed private file, bad
journal or unfinished checkpoint leaves access closed for owner intervention.
There is no public “ignore the gate” or force-unlock flag.

The transition journal is `<db>.privacy-transition.json` (128 KiB maximum).
Completion receipts are `<db>.privacy-transition.<owner-hash>.completed.json`.
The capture journal uses version 2 once it includes private routing decisions.
A plain capture-policy command cannot turn a private route back into shared.
Normal Recall restore removes any stale shared representation of those private
owners before publishing restored content. Updated readers refuse resurfaced
private content after raw content-only replacement. Raw restoration is not the
supported recovery workflow; a refused raw restore needs owner maintenance.

## Connection compatibility and limitations

Schema 12 establishes the new reader boundary. The previous schema-11 candidate
reader **and writer** refuse it. This supplements, rather than replaces, the
requirement to stop old clients before privacy maintenance. Older installed
binaries do not participate in the new leases or understand the external routing
journal. Raw SQLite/file access is outside the cooperative enforcement boundary.

On supported POSIX hosts, writers create `<db>.access.lock` with mode 0600. Readers open that file without
creating or changing it and hold a shared OS lock for their connection lifetime.
A copied store lacking its lease needs the matching writer/doctor setup before
readers can use it. Keep access, capture-policy and transition metadata with the
operational store. Never delete locks/journals to work around a refusal.

Vacuum and FTS rebuilding clean active database structures; they are not forensic
erasure. Original transcripts, old backups, exports, SSD snapshots, filesystem
copies and content already given to models remain outside this operation.
Owner-only file permissions do not separate agents running as the same OS user.
No OS sandbox or native conversation authentication is created by this module.

## Receipts and remaining work

The local `session-privacy/` review packet contains fault tests at five committed
transition points, partial-copy and empty-file publication tests, busy-reader/
writer tests, wrong-scope and tampered-copy refusal, stale restore tests, retained
settings/staging/annotations tests and exact non-owner data hashes from a real
store copy. No live source was converted.

A separate native Claude fixture used two per-invocation private MCP connections
and a third shared reader. Each owner received only its own synthetic block and
the shared reader received neither. Session IDs matched the launch configuration.
The fixture disabled built-in/delegation tools and used a local scripted provider:
it is a transport/connection receipt, not model-quality evidence or proof of a
full coding workflow. It does not certify Codex, Desktop Chat or Cowork isolation.

Version 2.5 includes a dedicated new-session launcher, pause/resume,
revocation/regrant, reviewed deletion and excerpt disclosure, and private
backup/restore. Shared app connectors remain unavailable for session-only memory.
Native fixture, fresh model and final activation evidence are tracked separately;
this conversion receipt does not certify a different host or a live activation.

The current privacy-maintenance implementation requires POSIX file locks and has
been exercised on macOS. Hosts without those locks retain ordinary unprotected
shared capture/read/import behavior, but refuse privacy maintenance or access to
a policy-bearing store. The fallback has simulated unit coverage; it is not a
Windows host-validation claim.

MCP diagnostics record busy leases as `store_busy` and incomplete transitions as
`store_unavailable`, rather than miscounting these conditions as invalid queries.
