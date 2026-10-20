# Durable memory: implementation and rollout

Use the [installation and upgrade guide](install-and-update.md) for setup, coordinated migration and reader refresh. This document describes durable storage and retrieval; the [changelog](../CHANGELOG.md) records release status.

Version 2.5.0 adds a source/block/passage store alongside the v2.4 exchange store. The new search interface retrieves historical evidence with exact references; the host agent writes the explanation and checks current facts separately.

## What changed

- Claude hooks retain complete redacted user/assistant text blocks and tool-call inputs. Passage size and display limits no longer determine how much text survives.
- `find` retrieves passages with agent, session, timestamp, block ID and character offsets. `get` supports exact pagination and neighboring messages.
- Prose is searched by default. `--kind tool_use` searches commands/inputs; `--kind all` includes both. Terms are quoted for FTS, common English question words are omitted, and `--require-all` selects conjunction instead of the default disjunction.
- `brief` selects the opening ask, recent evidence and passages containing decision/failure/open-work language. It is a bounded heuristic sample, not a comprehensive summary or a classifier of verified decisions. Head and tail excerpts carry offsets. `--live-git` puts an independently observed branch/commit/worktree state in a separate field.
- Explicit Claude and Codex JSONL adapters share agent-qualified source IDs. Normalized Git remotes group equivalent SSH/HTTPS clones; common Git directories group local worktrees. `--cwd` can correct absent or unsuitable source metadata.
- `status`, `sources` and `doctor` expose source coverage, cursors, backlog, last capture time, omitted/malformed counts and integrity checks. Empty search results do not prove absence from unimported histories.
- One optional local sentence-transformers backend can build vectors and fuse rankings with lexical search. Hooks never load a model. It remains opt-in after mixed results in the maintainer’s retrieval probes; those measurements do not establish general answer quality.

Use the [install/update checklist](install-and-update.md) for current rollout steps.

## Try it without changing the live database

Run from this repository; Python 3.9+ with SQLite FTS5 is required. Default paths need no third-party Python packages.

```bash
python3 scripts/recall_memory.py --db /tmp/recall-trial.db index /explicit/trace.jsonl --agent claude --cwd /path/to/project
python3 scripts/recall_memory.py --db /tmp/recall-trial.db index /explicit/codex-history --agent codex --cwd /path/to/project
python3 scripts/recall_memory.py --db /tmp/recall-trial.db search "why rejected batching" --cwd /path/to/project
python3 scripts/recall_memory.py --db /tmp/recall-trial.db get BLOCK_ID --start 0 --max-chars 8000 --neighbors 2
python3 scripts/recall_memory.py --db /tmp/recall-trial.db brief --cwd /path/to/project --live-git
python3 scripts/recall_memory.py --db /tmp/recall-trial.db doctor
```

`--db` precedes the operation. A timed import reports committed progress; repeat it without `--rebuild` to resume. Directory discovery and any single oversized JSONL record can exceed the soft time budget. Unterminated final records wait for their newline.

Current source adapters exclude reasoning, system/developer messages, tool results, image blocks and inline base64 data URIs. Other structured or unsupported records count as omitted. They do not install continuous Codex capture. Tool inputs and ordinary prose can still contain unrecognized sensitive material: [redaction is pattern based](../PRIVACY.md).

## Storage and recovery

Schema 12 preserves available legacy rows and durable data; durable tables were introduced at schema 6 and retained revisions at schema 10. Existing sessions get an independent backfill cursor when their hooks next run; bulk backfill is explicit. The old capped text cannot recover a discarded tail unless its original transcript is still available.

Source IDs combine agent and session. Message IDs come from the trace where available, otherwise its byte position. Block IDs derive from source/message identity, content ordinal and kind; repeating an import or rebuilding the same source does not duplicate blocks. Passage offsets refer to the retained redacted text, not original unredacted characters. Byte ranges identify source JSONL records.

Missing originals leave retained evidence readable. Shrinkage and changes to the 256 bytes preceding a saved cursor stop incremental capture and request `--rebuild`; this is an append-continuity check, not a full-file tamper detector. Earlier edits that leave that window unchanged require explicit rebuild. Rebuilds stage a complete candidate and publish it atomically at EOF through explicit indexing. Hooks can stage but never publish. Search and compaction recovery use the previous published text until completion; revision-specific get can recover retained older text afterward.

`prune AGENT:SESSION` removes a durable source, chunks, FTS entries and vectors in the same transaction. It leaves original transcripts and legacy exchange rows. Legacy session pruning also removes matching durable sources. `export AGENT:SESSION` emits complete redacted blocks and provenance as `recall-blocks-v1` JSON by default. Add `--include-revisions` for `recall-blocks-v2` with retained revisions and pins. Use `import-export` to restore this format; it is not transcript JSONL.

Make a SQLite backup before installing against an important live database. The schema upgrade is additive, but reverting the plugin does not remove new retained data or undo the migration. Install new runtime files before refreshing clients; generated app readers and loaded MCP processes must also be updated. See the install/update checklist rather than assuming a plugin reload updates every consumer.

## Optional semantic experiment

Install sentence-transformers separately if desired, and supply a trusted model directory that already exists locally:

```bash
python3 scripts/recall_memory.py --db /tmp/recall-trial.db semantic-build --model-path /existing/local/model
python3 scripts/recall_memory.py --db /tmp/recall-trial.db search "reason for the earlier choice" --all --semantic
```

Model files are fingerprinted; a changed model refuses retrieval until rebuilt. Source changes invalidate their vectors, and a build only attaches vectors to the exact text encoded. New passages need another explicit build. Semantic search scans the scoped vector corpus in memory; this is a small-corpus experiment, not a scalable approximate-nearest-neighbor service. No confidence threshold turns retrieved similarity into a factual answer.

## Validation scope

Validation covers migration preservation, complete long-answer pagination, partial
UTF-8/JSONL recovery, bounded progress, changed/missing sources, agent isolation,
redaction before FTS, cascade deletion, durable SessionEnd backfill, explicit
prose/command selection, model changes and capture during embedding. Real-history
rehearsals use isolated temporary stores; raw histories are not release assets.

Practical human acceptance and fresh synthetic Claude/Codex behavior checks are
separate evidence. Neither establishes blind retrieval accuracy or general model
answer quality. See the [release validation summary](../CHANGELOG.md) and
[private-session support boundaries](session-privacy-hosts.md).

## Retrieval ranking in 2.6

Lexical search, including natural-language questions, retains the released BM25/recency order. A development-only capitalization/explanation hint was removed after it regressed real-history retrieval. Source text, spelling and capitalization are not treated as proof that a passage answers the question.

Optional hybrid retrieval uses the best position a block earned in either the lexical or semantic list, with agreement used to break ties. The selected passage comes from the channel that supplied its best rank, so a relevant later passage is not replaced by an unrelated opening excerpt. When both channels match overlapping passages of the same retained revision, their source span is combined up to 3,200 characters. This keeps a lexical explanation from disappearing behind a semantic match elsewhere in the same passage window; non-overlapping or larger spans keep the winning passage. This changes ranking only: the stored model fingerprint, vector format, schema and citation identity are unchanged.

No model is bundled or downloaded by Recall. Both BGE-small-en-v1.5 and Snowflake Arctic Embed XS can be supplied as local sentence-transformers models. A smaller model is not automatically a better retriever; compare it on representative questions before replacing an existing semantic index. These options do not affect default lexical search or hooks.
