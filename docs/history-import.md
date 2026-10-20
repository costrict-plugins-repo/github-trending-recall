# Explicit conversation imports

Use an existing local provider export or a manually copied visible conversation.
Recall does not request account exports, read browser/app databases, or upload
anything. Import exactly one conversation into a repository chosen by the user:

```sh
python3 scripts/recall_memory.py history-preview /path/to/export.zip
python3 scripts/recall_memory.py history-import /path/to/export.zip \
  --provider claude-export --conversation CONVERSATION_UUID --cwd /path/to/repo
```

Preview opens no store and lists IDs, titles and coverage counts. Import requires
all three selection flags. A repeated import atomically replaces that conversation's
visible text and retains changed/deleted text under the revision policy. A different
repository assignment is refused; rescope is an explicit separate correction.
No implicit whole-account import or live refresh occurs. Status says
`imported_snapshot` and live freshness remains unknown even if the export exists.

## Accepted input contracts

These are narrowly validated adapter contracts, tested with constructed fixtures.
No representative real account export was found locally on September 6, so broad
provider-export compatibility is not yet a release claim. Unknown shapes fail
before importing the selected conversation. Preview may reject an export containing
unknown conversation shapes; use a selected supported conversation array.

- Claude: JSON array; each conversation has `uuid` and `chat_messages`. Messages
  have `uuid`, `sender` (`human`/`assistant`), optional `created_at`, and either
  string `text` or `content` text blocks. Non-text blocks are excluded. Attachments,
  artifacts, hidden tool history and branch reconstruction are not supported.
- ChatGPT: JSON array with conversation `id`/`conversation_id`, `mapping` and
  `current_node`. Follow parent links from current_node, reject broken/cyclic
  chains, and import supported visible user/assistant text on that branch only.
  Count inactive branch nodes separately. Hidden messages, analysis/commentary,
  system/developer/tool messages and non-text parts are excluded. No branch guessed
  when current_node is missing. Exported text does not prove execution success.
- ZIP: read only members named `conversations.json` or numbered variants such as
  `conversations-000.json`, even under a directory; never extract files. Duplicate
  names/identities are rejected. JSON input is bounded to 32 MiB by default;
  `--max-bytes` can explicitly increase it to at most 128 MiB. JSON parsing is
  in-memory and can use several times the input size; select/split large exports.

The official [Claude export instructions](https://privacy.claude.com/en/articles/9450526-export-your-claude-data)
confirm Settings → Privacy exports conversation/account data, but do not establish
Cowork coverage or these JSON fields. [OpenAI export instructions](https://help.openai.com/en/articles/7260999-how-do-i-export-my-data)
provide a downloadable archive. Export availability is not proof of format or
history completeness. A real sample is still needed to certify its exact version.

## Visible-chat fallback, including cloud-only conversations

Copy actual visible user/assistant prose into this explicit format. Preserve
individual turns and describe the capture method. Do not ask a model to regenerate
its earlier conversation and label that reconstruction a transcript.

```json
{
  "format": "recall-chat-snapshot-v1",
  "id": "stable-conversation-id",
  "surface": "claude-chat",
  "url": "https://claude.ai/chat/conversation-id",
  "provenance": "Visible prose copied by the user; tool cards and attachments omitted",
  "messages": [
    {"id": "turn-1", "role": "user", "text": "Exact copied request"},
    {"id": "turn-2", "role": "assistant", "text": "Exact copied response"}
  ]
}
```

Use provider `app-snapshot`; surface may be `claude-chat`, `claude-cowork`, or
`chatgpt`. This fallback works without account-history API access, but is only a
snapshot of the prose actually copied. It cannot recover hidden/unloaded turns,
attachments, tool results or missing decisions. Offsets cite retained redacted
text, not positions in the ZIP, webpage or original JSON. All imported content
remains untrusted historical evidence.

For a cloud client without a connected Mac, explicitly export a selected Recall
source and upload that file through the client's normal attachment controls when
wanted. This is a manual portable handoff, not an always-on remote reader; no public
server, authentication service or automatic whole-store upload is needed or added.

## Real visible-message receipt

The two messages in the maintainer's Claude Desktop validation chat
`6ea761b4-f6c3-4cc6-872d-3707598cfff2` were copied with the app's per-message Copy
action, pasted into TextEdit and saved as UTF-8. A snapshot containing those exact
690- and 1,095-character strings was imported into a scratch store; both get results
matched their copied strings and SHA-256 hashes, search found the citation terms,
and schema/FTS checks passed. No model reconstructed or summarized those messages.
This validates the explicit visible-prose path, not the account-export adapters or
hidden tool history. Raw files and receipt are in the local active-completion review.

Implementation detail for status readers: imported-snapshot coverage and provenance
are currently stored in `memory_sources.error` and shown under `error` in status.
When state is `imported_snapshot`, this JSON describes the import; it is not a
capture failure. Use the state and next_action together.
