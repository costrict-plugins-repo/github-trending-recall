# When recovery repeats searches or misses recent work

Separate access, capture, scope and evidence quality before changing search terms.

- A matching-scope MCP reader avoids shell access failures in sandboxed clients.
  Check its repository once. If CLI is needed and SQLite WAL access is denied,
  use the host's normal approval mechanism for that same read and scope. Reuse a
  working route for subsequent reads. Never repair a permissions failure by
  creating an empty store or bypassing SQLite coordination.
- Search/brief coverage includes `source_agents`, the registered-source counts by
  agent across the selected scope. A Claude-only scope does not contain a Codex
  task. `complete` describes checked files, not all histories on the machine.
  Full coverage lists sources and offsets; freshness checks remain paginated.
- Use the actual repository with `--cwd`, even if the shell started in a parent
  folder. A matching title does not establish repository identity. `--all` is
  appropriate for requested cross-project recovery, not for repairing missing
  capture. Source/date filters can distinguish an original decision from a
  recent question, worksheet or pasted report mentioning it.
- Keep JSON coverage and errors visible. Use `--limit` and `--max-chars` instead
  of piping output through `head`. Read the relevant decision and qualifications;
  continue pagination if they are incomplete or the full answer was requested.

## Selected capture and ongoing freshness

After choosing an authorized original transcript, import it explicitly:

```sh
python3 /path/to/recall/scripts/recall_memory.py index "/path/to/rollout.jsonl" --agent codex --cwd "/path/to/project" --seconds 30
```

Explicit `index --cwd` and foreground `recall_capture --cwd` now share the same
mapping contract. The mapping stays pinned when ordinary capture resumes. A
registered history in another repository returns `scope_mismatch`; inspect it
before using `rescope SOURCE --cwd DIR` to move its visibility. An update does
not automatically change previously registered scopes. Repeating an explicit
mapping for the same repository pins that existing source too.

The reader and installed skill do not themselves keep Codex history current.
A one-time import fixes the present backlog. For continuing capture, explicitly
run the foreground worker on selected files (or a deliberately selected root):

```sh
python3 /path/to/recall/scripts/recall_capture.py --db "/path/to/recall.db" --agent codex --path "/path/to/rollout.jsonl" --cwd "/path/to/project" --watch
```

It rotates unfinished sources and observes appends until stopped. This command
installs no service or host hooks; closing it stops refresh. For large staged
rebuilds, follow the reported index action to publish; do not repeatedly restart
with `--rebuild`. See [installation and update](install-and-update.md) and
[cross-agent capture](gpt-expansion.md).

## Scope of the September 7 correction

The local audit found justified recovery attempts searching incomplete or absent
histories. It also found unconditional full-block-reading instructions and
truncated output that hid coverage. The fixes preserve automatic skill selection
and the existing ranking algorithm. No blind accuracy or billed-token savings
are claimed. For optional session privacy, use the [dedicated private-session workflow](private-sessions.md).
Repository scoping is not isolation from another agent with filesystem access.
