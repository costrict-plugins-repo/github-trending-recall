# Retrieval probe

`evaluate.py` asks 60 questions derived from real Recall development history. The public fixture contains questions and short code/topic anchors; it contains no raw transcript. Supply an authorized local JSONL source yourself. The runner snapshots it into a temporary directory and builds a temporary database, so neither the live history nor the live Recall database changes.

```bash
python3 benchmarks/evaluate.py --trace /path/to/recall-session.jsonl
python3 benchmarks/evaluate.py --trace /path/to/recall-session.jsonl --model-path /existing/local/model
```

Each question's automatic labels are all assistant prose blocks containing its case-insensitive anchor. A hit means one of the top five retrieved blocks has that anchor. This is a weak retrieval diagnostic: repeated anchors can reward irrelevant blocks, paraphrases can be missed, and the script does not judge whether an answer is correct, complete, current or properly cited. Two of the 60 anchors were absent from the evaluated snapshot and excluded from hit-rate denominators. Latency includes retrieval only, not import, generation or model build.

## Local experiment, September 5, 2026

Corpus: one real Recall development session, 2,064 passages (including tool inputs). Optional model: locally cached `BAAI/bge-small-en-v1.5`, 384-dimensional vectors, CPU, fingerprint `55d82df3eec1e93c6890ed36a0914375e374b7fccfc33e25026cf7393ef4856f`. Building vectors took about 38–40 seconds on the development machine. No model was downloaded.

The fixture assigns the first 40 questions to development and the last 20 to `held_out`. The first run used both prose and tool inputs and no question-stopword filtering:

| Initial run | Labelled questions | Lexical hit@5 | Hybrid hit@5 |
|---|---:|---:|---:|
| Development | 39 | 23.1% | 23.1% |
| Reserved group | 19 | 36.8% | 36.8% |

Inspection showed that large tool payloads could crowd out prose. The default was changed to prose, with explicit tool searches, and common English question words were removed. Rerunning the **same** questions gave:

| Revised exploratory run | Labelled questions | Lexical hit@5 | Hybrid hit@5 | Median lexical / hybrid latency |
|---|---:|---:|---:|---:|
| Development | 39 | 48.7% | 59.0% | 5.6 / 79.3 ms |
| Previously reserved group | 19 | 52.6% | 42.1% | 5.6 / 78.2 ms |

Because both groups had already been inspected, the revised run has **no untouched holdout** despite the runner's `held_out` field name. The small single-session sample, broad anchor labels and multiple simultaneous retrieval changes prevent general quality claims or attribution of the gain to one change. This is also not a comparison against Funes or against the legacy v2.4 exchange interface.

Decision: keep lexical retrieval as the default and semantic retrieval opt-in. Embeddings improved one group and hurt the other while adding latency. The next useful evaluation is an independently authored set of questions spanning multiple sessions, explicit answer-bearing block labels, answer-absent cases and human judgments of evidence/citations. Do not tune repeatedly against this fixture and describe the result as held-out accuracy.

## Capture resource budgets (`capture_budget.py`)

`python3 benchmarks/capture_budget.py --transcript PATH [--concurrent N]` backfills a real transcript through the hook code path in a temp store (HOME and every `RECALL_*` override point at the temp dir), appends one turn for a steady-state pass, and exits 1 if any budget is exceeded: steady-state pass ≤ 25 ms, any pass ≤ 1 s, peak RSS ≤ 100 MB, database ≤ 0.5 × transcript bytes (vectors excluded). `--concurrent N` runs N hook processes against one store and reports lock errors, row totals and `PRAGMA integrity_check`. See the [durable memory guide](../docs/durable-memory.md) for capture and rebuild behavior; these local fixture budgets are not universal guarantees.

## Frozen labelled evaluations (`evaluate_frozen.py`)

Use an explicitly supplied local fixture with a `.sha256` sidecar. The runner verifies the exact copied byte prefix of every source and resolves every positive source-line/message/ordinal label before retrieval. Claude UUIDs and Codex payload IDs or byte-offset keys are supported. A hash mismatch, incomplete import or unresolved label stops the run.

```bash
python3 benchmarks/evaluate_frozen.py --fixture /path/to/questions.json --validate-only
python3 -m pytest benchmarks/test_evaluate_frozen.py -q
```

Validation builds a temporary store but does not search, embed or evaluate the question set. For `recall-human-review-candidates-v1` fixtures, retrieval requires every case's `review_status` to be `accepted` and a `human_review_receipt` recording the actual review; update the sidecar hash after reviewed corrections. The receipt must come from a human response, not an agent-generated approval. This establishes label provenance, not human judgment of generated answers.

The legacy arm supports Claude sources only. Codex cases are reported as unsupported, and legacy exact block-ID scores are null because exchanges do not have durable block IDs. Its supported corpus and denominator therefore differ from the combined durable arm; do not compare those aggregate percentages as an equal-corpus experiment. False-premise cases report returned evidence without claiming that retrieval itself rejected the premise. Exact labels, broad anchors and answer quality are distinct measurements.


## Independent MCP client check

`verify_mcp_sdk.py` uses the official Python MCP SDK 2.1.1 as a developer-only client against a synthetic scratch corpus. It exercises protocol negotiation, both source agents, Unicode pagination, repository boundaries, coverage, absence, a foreground watcher append and clean shutdown. The server and capture subprocesses run with `python -S`, so this check does not make the SDK a runtime dependency.

```sh
uv run --no-project --with 'mcp==2.1.1' python benchmarks/verify_mcp_sdk.py --out /path/to/receipt.json
```

This probe calls no model and does not use the private P10 question set. A protocol pass is not a coding-host model smoke test or a retrieval-quality score.
# Token/context cost probe

`token_cost.py --db SNAPSHOT --repo-id ID --out REPORT.json` measures static skill/tool-schema text, search/get/brief/status output and per-session compaction excerpts using `o200k_base`. It requires a separately installed measurement-only `tiktoken`; this is not a plugin dependency. Supply a frozen scratch store. The script does not run the human question packet, send model requests or record retrieved prose in its report. Fixed operational probes are not scored for relevance. Full retained-history totals are a size reference, not demonstrated savings against a competent alternative. Provider billing depends on the model, host serialization, repeated context and caching; this local proxy does not establish dollar savings.

`revision_budget.py --transcript PATH` measures an unchanged full rebuild in a
scratch store, including staging latency, explicit publication, peak RSS and disk
growth. It enforces stage-pass/RSS/schema checks, not a universal publication bound.

## 2.6 hybrid regression fixture and mandatory real-history gate

`evaluate_retrieval.py` imports synthetic Claude/Codex history into a temporary store, verifies retained message identity and text, and checks whether the first five hits include every required source record. Gold labels never enter query construction. This measures evidence availability, including records needed to reject a false premise; it does not score generated answers.

```sh
python3 benchmarks/evaluate_retrieval.py --fixture benchmarks/fixtures/retrieval-v26-dev.json --out dev-results.json
python3 benchmarks/evaluate_retrieval.py --fixture benchmarks/fixtures/retrieval-v26-holdout.json --out reserved-results.json
# Optional: add --model-path /existing/local/sentence-transformers-model
```

These two 32-case sets each contain eight sessions and 184 messages. They share templates, most evidence restates the topic name, and each topic has its own sessions. Their author also implemented the initial candidate. They are public regression data, not an independent holdout or sufficient release gate.

The rejected 2.6-dev.1 question hint scored 32/32 on both sets yet reduced real-history exact hit@5 from 24/25 to 12/25 unscoped and 15/25 scoped. Removing it restores the released lexical policy. The retained fusion and passage-selection fixes affect only optional hybrid search. Do not cite dev.1's synthetic lexical gain as a shipped improvement.

**Required for every ranking change:** run `evaluate_frozen.py` on the maintainer's authorized, frozen real-history fixture from the 2.5 review, for both the released baseline and candidate, using identical source prefixes and model fingerprints. Run scoped and unscoped lexical and hybrid arms; inspect per-case regressions as well as aggregate exact and broad scores. The 28-case fixture has 25 scored positives and three unscored false-premise cases. Its released lexical baseline is 24/25 exact and 22/25 broad in both scopes. Private histories and fixture paths must not enter public packages.

```sh
python3 benchmarks/evaluate_frozen.py --fixture /authorized/frozen/questions.json --model-path /existing/local/model --out candidate-real.json
python3 benchmarks/check_retrieval_gate.py --baseline released-real.json --candidate candidate-real.json
```

The gate verifies fixture/source/model identity and fails on any formerly passing exact or broad case becoming a miss, or on missing arms/cases. A failure blocks release until resolved or explicitly reviewed; a better aggregate does not hide individual regressions. A missing real-history receipt is an incomplete gate, not a pass. Baseline and candidate runners must come from retained committed code, and each report records runtime hashes. Freeze or commit code before live-model runs and retain the actual runtime snapshot; a hash alone does not preserve code.

Synthetic lexical evidence coverage is expected to remain at the released 31/32 development and 30/32 reserved levels after removing the hint. The portable runner exits nonzero for any missing required evidence, so these known misses are reported rather than silently credited. Passing hybrid synthetic checks and real-history gates does not establish general competitor parity, broad model answer quality or token savings. Continue to use new, independently authored histories for future improvements.
