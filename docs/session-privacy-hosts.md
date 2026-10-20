# Session privacy: supported attachment and validation

September 7, 2026. Recall 2.5.0, schema 12. Existing sessions remain shared unless explicitly converted. See the
[user workflow](private-sessions.md) and [conversion contract](privacy-conversion.md).

| Surface | Supported workflow | Evidence and boundary |
|---|---|---|
| Dedicated Claude Code controller | Fixed native root ID, one-turn print/resume, separate private MCP, lifecycle capture; standard file/shell tools; Agent/Task disabled | Native scripted fixture runs read tools and `pwd`, resumes the same ID and captures actual native history. Separate authenticated Opus synthetic checks passed; these are small behavior probes, not broad answer-quality validation. |
| Dedicated Codex controller | Native app-server root, host-supplied thread ID checked per dynamic call, native capture at turn/tool boundaries, resume; standard shell/filesystem sandbox | Native scripted fixture runs read tools and `pwd`, resumes the same root and captures actual native history. No implicit fork or subagent grant. The app-server dynamic-tool API is experimental and version-sensitive. |
| Ordinary Codex app/CLI sessions | Existing shared CLI/MCP reader; prepared opt-in lifecycle hooks | The preparer writes reviewable config and does not approve its own hooks. Enable through the host's normal `/hooks` review. The dedicated controller's direct capture does not require a hook-trust bypass. |
| Claude Desktop Chat / Mac-connected Cowork | Existing repository-scoped shared readers | No independently authenticated conversation identity on those connector routes; session-private activation is not offered there. |
| Arbitrary same-user processes | No OS isolation | Owner-only files do not separate agents running as the same account. Raw SQLite/transcript access can bypass the library. |

Only the dedicated controller profile establishes a supported private attachment.
An owner string in a tool argument, a task title, a directory or a project-wide
MCP config does not establish it. Models have no private maintenance tool, owner
selector, grant command or arbitrary native-flag passthrough. A foreign Codex
request is refused before capture or retrieval. A changed Claude identity or
transcript is refused. Native compaction does not authorize a new owner.

The capture-policy journal keeps shared suppression independent of content
backups. Separate private control records revoke reader generations independently
of restored content. Updated connections hold cooperative leases. Missing or
unsafe control/journal/lease files fail closed; deletion leaves a restrictive
owner tombstone. These mechanisms do not protect against obsolete clients or a
same-user operator deliberately replacing operational metadata. Stop old clients
before privacy maintenance, and do not downgrade policy-bearing deployments to
schema-10 writers.

No administrator access, Full Disk Access, remote service, permanent network
listener, hidden credentials or background capture daemon is added. POSIX locks
are required for privacy; macOS is exercised. Ordinary unprotected shared use has
simulated no-POSIX coverage, but no actual Windows privacy validation is claimed.

## Validation scope

Maintainer validation includes native-controller
receipts and the 900-test release suite, covering owner/foreign/no-identity and lifecycle tests, paused/deleted/revoked
access checks, interrupted conversion/deletion, safe backup restoration, selected
disclosure, fresh Codex model checks and resource/token measurements. Native
scripted providers validate software behavior and cannot establish model quality.
Fresh synthetic model checks are small behavior probes, not a broad benchmark.
The maintainer accepted the practical human retrieval review separately.

The ordinary Codex hook route still requires the owner to review and trust the
exact installed configuration in the native host. The dedicated private route
uses a controller-owned native transcript path and direct bounded capture; its
receipt is not presented as an ordinary global-hook activation receipt.

Official host contracts: [Codex hooks](https://learn.chatgpt.com/docs/hooks),
[Codex app server](https://learn.chatgpt.com/docs/app-server),
[Claude Code CLI](https://code.claude.com/docs/en/cli-reference),
[Claude Code hooks](https://code.claude.com/docs/en/hooks).
