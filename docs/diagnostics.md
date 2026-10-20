# Local diagnostics

Recall has `status` and `doctor` for coverage and store troubleshooting. Persistent operational logging is optional and stays on your machine. It adds no model calls, network service or runtime dependency.

Use `status` or `sources` for read-only coverage checks. `doctor` requires write
access even without `--repair`: opening it may migrate the schema, its FTS
integrity checks use insert commands, and it records an invocation. It is not a
read-only fallback for a sandbox permission error.

Enable it for an MCP reader by appending `--diagnostics /absolute/private-directory/recall-events.jsonl` to its launch arguments. The parent directory must already exist. Use separate filenames for separate readers if you want to distinguish them; events intentionally contain no repository or session identity. `prepare_claude_app.py` accepts the same flag and includes it in the generated Desktop snippet and Cowork plugin configuration.

The independent `recall_capture.py` command also accepts `--diagnostics PATH`; it records one event per completed capture cycle. Code hooks and legacy commands are not instrumented by this option. Leave the flag out to disable all diagnostics file creation. Removing it from an already-running reader requires restarting that reader; existing local logs remain until you remove them.

```sh
python3 scripts/recall_diagnostics.py /absolute/private-directory/recall-events.jsonl
```

The report includes retained event counts, outcomes, median/p95/worst latency and returned character totals. It can help distinguish empty searches, bad requests, busy stores, query-budget failures and maintenance problems. A successful search does not establish that its evidence was relevant or that the model's answer was correct. Character counts are not billed tokens; MCP hosts may serialize structured and text output differently.

Files are private (0600), capped at two 1 MiB JSONL segments and one empty lock file. The logger uses a nonblocking cooperative POSIX lock and drops an event if another writer holds it. On the first write failure in a process it emits a fixed stderr warning, never the error text. Unsupported locking also drops events; main Recall functionality continues. Storage is bounded, but a slow filesystem can still delay a write. Logs are a retained sample, not an audit ledger or a count of every Recall call.

The allowlist records event timestamp, operation, outcome, elapsed milliseconds, result count, output characters, capture passes/blocks/pending files and error count where applicable. Queries, passages, commands, source paths, identifiers, exception messages and credentials are never passed into the persistent schema. There is no anonymous client ID, remote collector, upload command or automatic sharing. Inspect any local report before choosing to share it.

Search and brief responses carry compact coverage counts. Call `recall_status` for the source details and follow its `next_offset` for additional pages. Compact counts describe the checked page; they do not claim every source on the machine was checked or captured.
