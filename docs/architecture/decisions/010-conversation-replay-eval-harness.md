# ADR 010 — Conversation-replay eval harness for compaction & retrieval changes

**Status:** Accepted
**Date:** 2026-04-27 (initial), amended 2026-04-28 (first-run findings + depth-pressure fixtures + retrieval-substitution-v1 implemented)
**Related:** [ADR 009](009-context-overflow-compaction.md) · [`docs/concepts/eval-baseline.md`](../../concepts/eval-baseline.md) · [`docs/research/models/model-research-3.md`](../../research/models/model-research-3.md) · [`docs/research/models/model-research-4.md`](../../research/models/model-research-4.md)

## Context

ADR 009 commits to a retrieval-first compaction strategy, with summary as a fallback. Before any code lands behind that decision, we need a way to answer one specific question: **does the chosen strategy actually preserve answer quality versus a full-history baseline, and does it beat the cheapest competitor (naive recent-N truncation)?** Without that signal, "we shipped retrieval-first compaction" is just code, not improvement.

The existing eval harness ([`docs/concepts/eval-baseline.md`](../../concepts/eval-baseline.md), [`backend/tests/eval/runner.py`](../../../backend/tests/eval/runner.py)) measures retrieval quality only — it scores `retrieve()` outputs against `expected_paths`, with a frozen baseline JSON ([`baselines/step-28c.json`](../../../backend/tests/eval/baselines/step-28c.json)) and an opt-in pre-merge gate ([`test_baseline_floor.py`](../../../backend/tests/eval/test_baseline_floor.py)). It has no concept of conversation, compaction, or end-to-end answer quality. Bolting conversation replay onto it would conflate two separate evaluations; building a sibling harness that mirrors its discipline (frozen baselines, stable-key JSON, opt-in env-var gate) keeps both stable.

The trap to avoid: writing the feature, then writing the eval. Numbers stop being credible the moment the developer has a stake in their direction. **Freeze the baseline, swap strategies behind it, diff.**

## Decision drivers

1. **Counterfactual against the cheapest baseline.** Retrieval-first compaction must be measured against naive recent-N truncation. If naive truncation scores within noise of full-history on long-conversation fixtures, ADR 009's retrieval-first stance has to be revisited before code ships.
2. **Determinism.** Same fixture + strategy + model + seed must produce byte-identical output. Without that, before/after numbers are noise.
3. **Diffable baselines.** Match the existing `step-28c.json` pattern — committed JSON, sorted keys, stable per-fixture record. `git diff` is the regression review.
4. **Mechanical floor + judged supplementary signal.** Per-turn `expected_facts` checks (string/fuzzy match) are the load-bearing signal. LLM-as-judge is supplementary, used to detect quality regressions mechanical assertions miss (voice, coherence, partial-answer fluency).
5. **No self-judging.** The chat model is biased toward outputs that look like its own; a cross-model judge (different family or strictly stronger same-family) is the right tool.
6. **Chat under test runs locally; judge runs wherever is practical.** Customer machines never run this; dev hardware does. ADR 002's no-cloud constraint applies to shipped product, not to dev infra. The chat model under test stays local — it's what we ship and we want to evaluate the actual production stack. The judge model has no shipped-product equivalent: it's a measurement tool. Running it as a hosted API call (Claude / GPT) is acceptable for eval-only use, since fixtures are hand-authored and contain no customer data. The local 235B judge is the upgrade path once Tier C dev hardware exists, not a launch prerequisite.
7. **Pluggable strategies.** Same harness must evaluate ADR 009 (compaction) and any future swap (e.g., retrieval-substitution-v2, summary-fallback variants). Strategy is a first-class abstraction.

## Decision

### Fixture schema

Conversation fixtures live at `backend/tests/eval/conversations/fixtures/*.json`. Each fixture is hand-authored, committed, and frozen — the same discipline as [`queries_reference.py`](../../../backend/tests/eval/queries_reference.py).

```json
{
  "id": "long-conv-tool-recall",
  "description": "Turn 18 references a tool result from turn 6. Tests that compaction preserves tool outputs the user can refer back to.",
  "tags": ["long-conv", "tool-recall"],
  "preconditions": {
    "workspace_fixture": "reference_workspace",
    "tool_mocks": "long-conv-tool-recall.tools.json"
  },
  "turns": [
    {
      "role": "user",
      "content": "...",
      "expected_tool_calls": [{"name": "read_note", "args": {"path": "..."}}]
    },
    {
      "role": "assistant_target",
      "expected_facts": [
        {"id": "fact_a", "match": "regex", "pattern": "(?i)contract.*expires"},
        {"id": "fact_b", "match": "fuzzy", "text": "the renewal clause says 90 days", "min_score": 0.75}
      ],
      "must_not_contain": [
        {"match": "regex", "pattern": "(?i)confabulated|hallucinated"}
      ]
    }
  ]
}
```

`role: assistant_target` turns are where the harness scores. Other assistant turns may exist in the fixture for context but are not scored. Tool calls are matched against a deterministic mock layer; tool invocations not listed in `tool_mocks` fail loudly rather than calling real tools.

### Strategy interface

A `ContextStrategy` is the unit of swap. Strategies live at `backend/tests/eval/conversations/strategies/`:

```python
class ContextStrategy(Protocol):
    name: str  # stable id, embedded in baseline filenames
    def assemble(self, history: list[Turn], retrieval_engine: Retrieval, budget: TokenBudget) -> AssembledContext: ...
```

Launch strategies:
- **`full-history`** — pass every turn verbatim, no compaction. Reference baseline.
- **`naive-truncate-N`** — keep the last N turns verbatim, drop the rest with no replacement. Cheapest competitor. The strategy ADR 009's stance must beat.
- **`retrieval-substitution-v1`** — ADR 009's retrieval-first strategy. Drops older turns; on each new turn, re-runs retrieval over the vault scoped by recent-window terms; substitutes retrieved content for the dropped turns.
- **`summary-fallback`** — ADR 009's summary-fallback variant. Used when retrieval substitution underperforms (D14 fork resolution).

The same interface ships in production (`backend/services/chat/context_strategy.py`) — the eval and the production path share the abstraction. This is the only production-side change ADR 010 introduces.

### Pinned chat model

The eval pins **Qwen3-30B-A3B (May 2025 hybrid base)** at Q4_K_M with `/no_think` directive — the v1 canonical chat model. Every cross-strategy comparison runs against this single model. Without the pin, deltas conflate "strategy worked" with "model was slightly different."

**Downgrade-ladder sweep** is a secondary run (lower frequency — once per release, not per PR) covering Qwen3-14B and Qwen3-8B against the same fixtures. Catches the case where compaction works on the 30B-A3B but degrades on the smaller fallbacks.

Pinned: model, quant, temperature (0.0 for the eval; production runs at higher temperature, but eval needs determinism), seed, `keep_alive: -1` for the eval run, embedding model (`Qwen3-Embedding-0.6B`), reranker if any, clean reference workspace per fixture (existing [`setup_reference.py`](../../../backend/tests/eval/setup_reference.py) mechanism).

### Judge

The judge model is conditional on the eval-side hardware available. Three configurations, ranked by current practicality given the project's actual dev hardware (24 GB Apple Silicon as the primary) and a "judge is supplementary" posture:

**Default (current hardware) — hosted Claude or GPT, eval-only.** A frontier-tier hosted model (e.g., `claude-opus`, `claude-sonnet`, `gpt-4`-class) called from the runner via API on the developer's machine. This is acceptable under ADR 002 because:
- The eval is dev infra; nothing in this code path runs on customer machines.
- Fixtures are hand-authored; they contain no customer data, no internal IP, nothing that wouldn't be acceptable to send to a third party.
- The judge is a measurement tool, not a shipped-product component — it has no analogue in production behavior.

The cost is small: ~19 fixtures × 3 strategy pairs × 2 orders × 3 seeds ≈ 340 judgments per full-grid run at the current suite size, at roughly 5K-in / 200-out per judgment — still pennies per run. Scales linearly with fixture count.

**Upgrade path — local Qwen3-235B-A22B-Thinking-2507 (Tier C, Apache 2.0).** Once Tier C dev hardware (Mac Studio Ultra 192 GB / multi-GPU rig / dedicated cloud GPU box) is available, swap the judge to local. Same protocol, same scoring, just a different judge endpoint. The judgment-protocol abstraction (below) makes the swap a single config change.

**Tier-B intermediate — local Qwen3-Next-80B-A3B-Thinking** (~50 GB Q4, fits 64 GB unified Mac). Lower judge ceiling; still cross-model and same-family. Available as the upgrade path if Tier B dev hardware exists before Tier C.

Self-judging (Qwen3-30B-A3B judging Qwen3-30B-A3B outputs) is rejected across all three configurations. The bias is well-documented in MT-Bench / Chatbot Arena follow-up work: models prefer outputs that look like their own, and can't reliably distinguish good-from-bad of themselves because their quality preferences and their production preferences are the same distribution. Tolerable noise on absolute scores; unacceptable for measuring deltas between strategies.

**Implementation requirement:** the runner abstracts the judge behind a `JudgeProvider` interface. Default impl is the hosted-API call (`AnthropicJudgeProvider` or `OpenAIJudgeProvider`); local impls (`OllamaJudgeProvider` for the 235B / Tier B fallback) live next to it. The runner `--judge-model` flag selects.

#### Judgment protocol

- **Pairwise, not absolute.** "Given this conversation context and these expected facts, which response (A or B) better answers the user's question?" Ties allowed. Pairwise is dramatically more reliable than 1–5 scoring.
- **Reference-grounded.** The judge sees `expected_facts` for the turn. This anchors the judgment on required content rather than stylistic preference.
- **Position-bias control.** Each pair is judged in both orders (A vs B, B vs A); a strategy only "wins" if it wins both orders. Disagreement counts as a tie.
- **Multi-seed.** Three judge runs per pair per order. Majority vote. Disagreement above threshold flags the fixture for human review.
- **Pairs evaluated per fixture-turn:**
  - `(full-history, strategy_under_test)` — does the strategy match full-history? The "preserves quality" signal.
  - `(strategy_under_test, naive-truncate-N)` — does the strategy beat the cheap baseline? The "earns its complexity" signal.
  - `(naive-truncate-N, full-history)` — control; how close does the cheapest baseline already get? The fork-resolution signal for ADR 009.

### Mechanical assertions

The mechanical floor runs independently of the judge. For each `assistant_target` turn:

- **`expected_facts`** — every fact listed must be present in the response, by regex or fuzzy match (`min_score` per fact). A mechanical pass = all facts present. A mechanical fail = at least one fact missing.
- **`must_not_contain`** — none of the listed patterns may appear. Catches confabulation against curated distractor scenarios.

The mechanical signal is hard. A response that fails mechanical fails the turn regardless of judge score. The judge is supplementary — it ranks among mechanically-passing responses.

### Replay runner

`backend/tests/eval/conversations/runner.py` drives evaluation. Inputs:

```
run_conversation_eval.py \
  --fixtures all \
  --strategies full-history,naive-truncate-8,retrieval-substitution-v1 \
  --chat-model qwen3-30b-a3b-q4 \
  --judge-model qwen3-235b-a22b-thinking-2507 \
  --seeds 3 \
  --out backend/tests/eval/conversations/baselines/<run_id>.json
```

Per fixture × strategy: replays each user turn through the real chat pipeline with the strategy injected, captures the assistant response, scores `assistant_target` turns mechanically, runs the judge protocol against pairs, captures p50/p95 latency, captures token cost.

### Output format (frozen-baseline JSON)

Stable-key JSON, mirroring the existing `step-28c.json` pattern:

```json
{
  "run_id": "...",
  "chat_model": "qwen3-30b-a3b-q4",
  "judge_model": "qwen3-235b-a22b-thinking-2507",
  "strategies": ["full-history", "naive-truncate-8", "retrieval-substitution-v1"],
  "fixtures": ["distractor-injection", "long-conv-shallow", "long-conv-tool-recall", ...],
  "overall": {
    "mechanical_pass_rate":   {"full-history": 1.00, "naive-truncate-8": 0.62, "retrieval-substitution-v1": 0.94},
    "judge_win_vs_full":      {"naive-truncate-8": 0.31, "retrieval-substitution-v1": 0.78},
    "p95_turn_latency_ms":    {"full-history": 12300, "naive-truncate-8": 4100, "retrieval-substitution-v1": 4900}
  },
  "by_fixture": { "...": { ... } },
  "details":    { "...": { ... } }
}
```

`overall` and `by_fixture` are sorted keys; `details` carries full per-turn responses for human review. The diff that matters for ADR 009: `judge_win_vs_full` for `retrieval-substitution-v1` versus `naive-truncate-8` on long-conversation fixtures.

### Frozen baselines and pre-merge gate

Baselines live at `backend/tests/eval/conversations/baselines/<strategy>/<fixture>.json` and `…/<run_id>.json` for full-grid runs. Committed; updating is a deliberate act, never to make a failing test pass.

`backend/tests/eval/conversations/test_conversation_floor.py` enforces, opt-in via `JARVIS_CONVO_EVAL=1`:
1. **Mechanical pass rate** for the strategy under test must not drop more than **5%** per fixture vs its own committed baseline.
2. **Judge win-rate vs full-history** for the strategy must not drop more than **10 percentage points** per fixture vs baseline.
3. **p95 turn latency** must not regress more than **20%** vs baseline.

Skips silently in ordinary CI (no env var, no reference workspace fixture). Functions as a hard pre-merge gate when developers run it locally before merging any retrieval/compaction-affecting change. Same operational pattern as [`test_baseline_floor.py`](../../../backend/tests/eval/test_baseline_floor.py).

### Determinism kit

- Pinned chat model + quant + temperature 0 + fixed seed.
- Pinned judge model + temperature 0 + three fixed seeds.
- Pinned embedding model.
- Tool-call mock layer (`backend/tests/eval/conversations/mocks.py`); unmocked tool calls fail loudly.
- Clean reference workspace per fixture (existing mechanism).
- `keep_alive: -1` for eval models so model-load cost doesn't pollute latency measurements.
- Random / time / UUID generation seeded per fixture.

### Fixtures (currently 19 — original 10 + 5 stress + 4 depth-pressure)

Hand-authored, designed against the failure-mode taxonomy. The original ten landed at first-cut; the suite has grown twice in response to specific gaps the early runs surfaced (see "First baseline run" amendment below).

**Original 10 launch fixtures.**

| # | Fixture | What it stresses |
|---|---|---|
| 1 | `long-conv-shallow` | 30-turn casual chat; late turn references a turn-3 detail |
| 2 | `long-conv-tool-recall` | Tool result from turn 6 referenced at turn 18 |
| 3 | `distractor-injection` | Earlier turn that retrieval might surface incorrectly; tests confabulation rate |
| 4 | `multi-tool-loop` | Single turn with a complex multi-round tool-loop, then follow-up about it |
| 5 | `cross-lingual-polish` | Polish chat (validates Qwen3 multilingual claim under compaction) |
| 6 | `rapid-context-refill` | Long pasted content (e.g., a contract section) early; referenced late |
| 7 | `note-overlap` | Conversation about a vault note; tests retrieval-from-vault vs retrieval-from-history disambiguation |
| 8 | `technical-followup` | Engineering-domain Q deriving from earlier turn (numerical chain) |
| 9 | `pivot-mid-conversation` | User changes topic; old-topic must not bleed into new |
| 10 | `session-boundary-ambiguity` | "We discussed earlier" near the compaction boundary |

**Added in the higher-rigor pass (5 stress fixtures).**

| # | Fixture | What it stresses |
|---|---|---|
| 11 | `fifty-turn-marathon` | 50-turn conversation; tests behavior beyond the typical chat-slot context window |
| 12 | `multi-target-derivation` | Several scored turns deriving from chained earlier turns |
| 13 | `code-domain-debugging` | Code identifier recall in a multi-function file (function-name precision) |
| 14 | `numerical-correction` | User self-corrects a value mid-conversation; recall must use the corrected version |
| 15 | `code-switch-pl-en` | Mid-conversation language switch (Polish → English) preserving identifier fidelity |

**Added 2026-04-28 (4 depth-pressure fixtures, ≥17 user turns each, anchor at turn 0).** These exist specifically to fail under `naive-truncate-16` and pass under `full-history`; without them the suite's gate signal was bounded by fixtures topping out at 14–16 user turns. Unit-test guards pin two properties per fixture: (a) `naive-truncate-16` actually drops the buried token from the assembled context, and (b) `retrieval-substitution-v1` at recent-N=4 actually re-introduces the token via keyword overlap.

| # | Fixture | What it stresses | Anchor token |
|---|---|---|---|
| 16 | `deep-name-recall` | Identifier (codename) recall at depth-19 | `Albatross-9` |
| 17 | `deep-number-recall` | Numerical recall (£ amount) at depth-19 | `47,500` |
| 18 | `deep-decision-recall` | Tech-stack decision + rejected alternative | `PostgreSQL` |
| 19 | `deep-multi-detail-recall` | Two distinct facts (client + date) at depth-18 | `Northwind` |

19 fixtures × 7 strategies × 3 seeds = 399 calls per full grid. At roughly 6–18 s per call on Qwen3-30B-A3B with `think: false` on Apple Silicon, that's 40 min – 2 h wall-clock per run on Tier-A dev hardware. Pre-merge gate runs use a subset (changed strategy only); full grid on release. Add a fixture every time real usage produces a regression the existing suite didn't catch — same growth discipline as `queries_reference.py`.

## Alternatives considered

### A. Skip the harness; ship the feature; watch real usage
Rejected. Real-usage signal exists (manual re-include rate, thumbs-down on compacted-response turns) but lags by weeks and can't isolate cause. The exact concern that motivated this ADR — "are we improving or just adding code?" — is what an offline harness exists to answer before the code lands.

### B. Extend the existing `runner.py` in place
Rejected. The current runner is retrieval-only; bolting conversation replay onto it conflates two evaluations. Sibling subdirectory (`conversations/`) keeps the existing baseline stable while we build the new one.

### C. Self-judging (Qwen3-30B-A3B judges its own outputs)
Rejected. Bias is well-documented and load-bearing for delta measurement, even if tolerable for absolute scores. The whole point of this harness is measuring deltas between strategies on the same chat model.

### D. LLM-judge only, skip mechanical assertions
Rejected. Judges drift, anchor on fluency, can't catch confabulation against ground truth. Mechanical `expected_facts` per turn is the floor; judge is supplementary.

### E. Mechanical only, skip judge
Rejected. String/fuzzy match passes when the answer is technically correct but incoherent or off-tone. Both signals together separate "right facts, wrong shape" from "right shape, wrong facts."

### F. Hosted Claude / GPT as judge (eval-only — not shipped)
**Accepted as the default for current hardware.** The original draft rejected this; revisited and flipped because the project's actual dev hardware is 24 GB Apple Silicon and neither the local 235B (Tier C) nor the 80B fallback (Tier B) fits. Blocking eval work on hardware that doesn't yet exist costs more than the trade we'd make by adopting a local judge. The hosted judge runs on dev hardware against hand-authored fixtures with no customer data; ADR 002's no-cloud constraint scopes to shipped product. The local 235B judge is recorded as the upgrade path, swappable behind the `JudgeProvider` interface once Tier C dev hardware exists. The "the eval depended on a hosted API that changed" risk is real but small at ~180 judgments per run, with the protocol abstracted enough that switching providers (Anthropic → OpenAI → local) is a config change, not a rewrite.

### G. Absolute (1–5) scoring instead of pairwise
Rejected. Absolute scoring is dramatically less reliable than pairwise across the LLM-judge literature. The implementation cost difference is small.

## Consequences

### Positive
- The fork from ADR 009 ("does retrieval-first beat naive truncation, or just match full-history?") becomes answerable before code lands.
- Every future compaction or retrieval-substitution change has a frozen comparison point.
- Strategy abstraction in production (`ContextStrategy`) is small, justifiable, and useful beyond eval — different compaction policies for different workloads can attach later without re-shaping the chat path.
- The eval runs entirely on the local Apache-2.0 stack we ship; no hosted-API dependency creeps in.
- Determinism kit forces explicit treatment of every randomness source — useful in production debugging too.

### Negative
- Wall-clock cost: chat replay dominates because the chat model runs on local hardware. On 24 GB Apple Silicon with Qwen3-30B-A3B Q4_K_M, replay alone is roughly 1–2 hours for the full grid; judge calls (hosted API, parallelizable) add minutes. Pre-merge gates must be selective (run only the changed strategy's pairs per PR; full grid pre-release).
- Hosted judge introduces a vendor dependency for the eval pipeline. Mitigated by `JudgeProvider` abstraction, fixed-version model selection (`claude-opus-4-7` etc., not "latest"), and recorded judgments stored in baselines so historical runs are replayable even if the hosted model is later deprecated.
- Hosted judge means a small per-run API cost. Pennies per full-grid run, but it's not zero. Tracked in the open follow-ups for budget visibility.
- Hand-authored fixtures are a fixed cost; the launch ten plus ongoing growth (currently 19, see Fixtures section) is a discipline obligation. Mitigated by the "grow on real-usage failure" pattern that already works for `queries_reference.py`.
- Fixtures freeze a snapshot of expected behavior. Schema migrations (e.g., new tool surface) may invalidate fixtures and require re-recording. Version fixtures with the schema, document re-recording in this ADR's open follow-ups.

### What this changes about existing code
- New subtree `backend/tests/eval/conversations/` (runner, fixtures, baselines, mocks, floor test).
- New abstraction `backend/services/chat/context_strategy.py` defining `ContextStrategy` Protocol; existing compaction logic in [`backend/routers/chat.py`](../../../backend/routers/chat.py) (`_compact_stale_tool_results`, the implicit "full history" assembly) refactors behind it. Production path defaults to the same strategy that scored best in the eval.
- New `backend/tests/eval/conversations/mocks.py` for deterministic tool-call replay.
- Update [`docs/concepts/eval-baseline.md`](../../concepts/eval-baseline.md) to cross-link to the conversation harness.
- New `docs/concepts/conversation-eval-harness.md` — created when the runner code lands, not in this ADR.
- [`docs/.registry.json`](../../.registry.json) — new feature entry for `conversation-eval-harness` when source files exist.

### What this does NOT change
- The existing retrieval-only harness (`runner.py`, `step-28c.json`, `test_baseline_floor.py`) is untouched. It continues to gate retrieval changes independently.
- ADR 009's compaction strategy is not yet validated. It's the first thing this harness measures. If the numbers don't support retrieval-first, ADR 009 gets amended.
- Production chat path doesn't change behavior on day one. The strategy abstraction lands; the default strategy reproduces today's behavior bit-exact.

## Decision gate (the load-bearing fork)

After the harness lands and the launch fixtures are baselined, the comparison that matters. **Mechanical pass-rate is the primary signal; judge win-rate is consulted only when mechanical signal is ambiguous (delta < ~3 percentage points across fixtures).** The judge is supplementary, not gating.

- **If `naive-truncate-8` scores within ~5 mechanical-pass-rate-points of `full-history` on long-conversation fixtures** — ADR 009's retrieval-first stance must be revisited. The complexity isn't earning its keep. Amend ADR 009 to default to recent-N truncation; retain retrieval-substitution as opt-in for fixtures where it demonstrably wins.
- **If `retrieval-substitution-v1` clears `naive-truncate-8` by ≥5 mechanical-pass-rate-points on long-conversation fixtures** — ADR 009 stands. Ship it as default.
- **If neither strategy preserves answer quality vs full-history** — neither compaction approach is mature enough. Push the context boundary instead (e.g., reduce default chat-slot context to fit cleanly under RULER-safe limits; defer compaction).
- **If mechanical signal is within ~3 points across the candidate strategies** — invoke the judge for fluency / coherence / partial-correctness deltas. This is the only path on which the hosted judge is load-bearing for the gate decision; otherwise the eval can run on mechanical alone.

Naming this gate explicitly is the point of the ADR. The numbers decide; the developer doesn't.

## Issue 4 (2026-04-28 evening) — Qwen3-30B-A3B think:false leak; canonical chat model swap to Qwen3-14B

The latency benchmark harness ([ADR 011](011-latency-benchmark-harness.md)) ran its first PR-scope baseline on M5 Pro 24 GB and surfaced a finding that invalidates the original "Qwen3-30B-A3B is the v1 canonical chat model" pinning above:

**On Ollama 0.18.0 (the version pinned by Issue 1's regression note), `think: false` does not suppress chain-of-thought emission for `qwen3:30b-a3b`.** Direct reproduction:

```bash
curl -s http://127.0.0.1:11434/api/chat -d '{
  "model": "qwen3:30b-a3b",
  "messages": [{"role": "user", "content": "Say hi in one word."}],
  "stream": false, "think": false,
  "options": {"num_predict": 8}
}' → content: "Okay, the user asked me to say"
```

The model burns 8 tokens of decode budget on internal monologue and never reaches the answer. `_strip_thinking` is a no-op when `</think>` doesn't appear (because the cap stopped generation first). The same prompt against `qwen3:14b` and `qwen3:1.7b` produces clean `"Hi."` in 3 tokens.

A `stop=["<think>"]` workaround was tested and fails because Ollama 0.18.0 already strips the opening `<think>` tag from the stream — there's no literal text to match.

**Latency impact.** PR-scope benchmarks on the same machine, same scenarios:

| Scenario | Metric | qwen3:30b-a3b | qwen3:14b | Δ |
|---|---|---:|---:|---:|
| warm-short | total p95 | 1177 ms | 299 ms | **−75%** |
| chat-realistic-shallow | TPS p50 | 5.5 | 14.0 | **+155%** |
| chat-realistic-shallow | total p95 | 14.6 s | **1.8 s** | **−87%** |

The realistic-chat scenario going from 14.6 s to 1.8 s is not an optimization — it's a fix for a broken UX where users were watching the model "think out loud" before answering.

**Hardware-fit secondary win.** Qwen3-14B Q4_K_M is ~9 GB versus 30B-A3B's ~17 GB. On a 24 GB Apple Silicon machine the 30B-A3B leaves ~1 GB headroom (catastrophic — any other app spike triggers swap or OOM); 14B leaves ~15 GB headroom. This is the memory-pressure problem `effective_footprint_bytes()` was scaffolded for, manifesting at install time.

### Amendment

The canonical chat model pinning above ("Qwen3-30B-A3B (May 2025 hybrid base) at Q4_K_M with `/no_think` directive") is **superseded by Qwen3-14B at Q4_K_M** for v1 eval pinning. Specifically:

- [`backend/tests/eval/conversations/chat_adapters.py`](../../../backend/tests/eval/conversations/chat_adapters.py) `DEFAULT_OLLAMA_MODEL = "qwen3:14b"`.
- [`backend/tests/eval/latency/run_bench.py`](../../../backend/tests/eval/latency/run_bench.py) `DEFAULT_MODELS_PR = ("qwen3:14b",)`, nightly drops 30B-A3B.
- The "Pinned chat model" section above stands as the original intent; this amendment overrides it for the Ollama 0.18.0 era.

### Forward-compatible fix

The static "single canonical chat model" choice is the wrong shape long-term — different users have different Ollama versions, OS major versions, hardware, and RAM. Whether `think: false` works for a given model is environment-specific, not globally decidable. **[ADR 012](012-chat-model-self-test.md)** files the architectural answer: a self-test that runs on the user's machine at install (or on demand) and probes correctness + hardware-fit + speed for each candidate model, picking the best fit per environment. The 14B default in this amendment is the conservative bridge until the probe lands.

### Conversation-eval baselines invalidated

The first-baseline-run results below (15 fixtures × 5 strategies × 3 seeds against `qwen3:30b-a3b`) **measured a fundamentally broken model output** — the chain-of-thought leak means scored responses include thinking-prose noise that the strip caught for full responses but not for failure-mode samples. Those baselines are preserved for historical reference but should not be used as the comparison floor for any future strategy diff. The next conversation-eval grid runs against `qwen3:14b` and is the new canonical baseline.

## First baseline run (2026-04-28) — findings & amendments

The first full grid (5 strategies × 15 fixtures × 3 seeds = 225 model calls) ran against `qwen3:30b-a3b` Q4_K_M on Apple Silicon (Ollama 0.18.0; v0.21.x crashes on Apple M5 + macOS 26 with a llama.cpp-runner segfault — pinned to v0.18.0 until upstream fixes the regression). Two issues showed up that the harness did not anticipate at design time, and one substantive finding emerged from the run.

### Issue 1 — Qwen3 thinking-mode leak survives `think: false`

The eval harness sends `think: false` at the top level of the Ollama chat request to disable Qwen3's chain-of-thought decode (`/no_think` posture for the v1 chat model). On Ollama 0.18.0 this only partially honors the request: the model still emits the chain-of-thought, Ollama strips the opening `<think>` tag but leaves the prose plus the closing `</think>`, then concatenates the real answer. All 255 turns of the first run had a `</think>` in the response body.

The scorer regexes over the full response. Chain-of-thought routinely names rejected candidates ("the user wants `finalize_invoice`, not `rollback_to_draft`") that match `must_not_contain` patterns — producing false-positive `CONFABULATION` severities even on fixtures where the final answer was correct. Confabulation rate on the raw run was 35.6% across full-context strategies; after stripping chain-of-thought it dropped to 8.9%. The 26.7-pp gap was scoring artifact, not model behavior.

**Fix.** `OllamaChat` now applies `_strip_thinking()` to every response before returning to the runner: discards everything up to and including the last `</think>`. No-op on Anthropic responses or models not in thinking mode. Runner-side history then sees only the final answer (matching what production-with-/no_think would emit), so subsequent turns aren't polluted either. The strip is unit-tested in `test_chat_adapters.py` and doesn't launder real failures — guards still trigger when the post-strip text actually violates them.

### Issue 2 — Re-scoring without re-running the model

Once the strip landed, the existing baseline JSON needed re-interpretation: the raw responses were captured but their severities were wrong. A 30B grid takes 45 min – 2 h; re-scoring the same captured responses takes <1 s and produces the same answer.

**Tool.** `tests/eval/conversations/rescore.py` loads a baseline JSON, strips chain-of-thought from each `response_text`, re-runs `score_turn` against the matching fixture turn, and writes a fresh baseline tagged with `rescored_from_timestamp_utc` for lineage. This is the right way to fix a scoring-layer bug — model output is fixed, only our interpretation changed.

This generalizes: any scorer change (new fact patterns, tightened guards, different severity thresholds) can be re-applied to historical baselines without spending GPU minutes. Any future scorer change MUST be exercised against the rescore CLI before promotion.

### Issue 3 — First gate verdict (post-rescore, 15 fixtures × 3 seeds)

| Comparison | Δ (B-A) | 95% CI | Verdict |
|---|---:|---|---|
| full-history vs naive-truncate-16 | 0.000 | [0.000, 0.000] | **equivalent** |
| full-history vs naive-truncate-12 | -0.067 | [-0.200, 0.000] | equivalent (CI hits zero) |
| full-history vs naive-truncate-8 | -0.111 | [-0.311, +0.044] | equivalent |
| full-history vs naive-truncate-4 | -0.444 | [-0.689, -0.200] | **regression** |

Two readings.

**Headline:** the gate now correctly identifies aggressive truncation (recent-4) as a real regression — CI excludes zero. This is the load-bearing capability ADR 010 was supposed to provide; it works.

**Bounded "equivalent" verdict for moderate truncation:** naive-truncate-16 produces per-fixture-identical clean-pass rates to full-history on this fixture set. Naive-12 hits the upper CI at exactly zero — one more failing fixture and it would flip to regression. Two fixtures (`fifty-turn-marathon`, `technical-followup`) fail under every strategy including full-history, indicating model-limit behavior that the harness can't separate from strategy effects with current power. The verdict on naive truncation is "not refuted at n=15," not "validated."

**What this means for ADR 009's stance.** Premature to revisit. The harness doesn't yet have the power to distinguish "naive-16 is genuinely safe" from "the fixture suite doesn't pressure-test depth enough." Two concrete next steps before the gate signal counts as decisive:

1. **Author depth-pressure fixtures** that force a real dependence on >16 messages of history (e.g., a 30-turn conversation where the answer requires turn-3 detail with no retrieval re-introducing it). Expected outcome: naive-16 starts failing on these where full-history passes, the CI on naive-16 vs full-history then either excludes zero or doesn't.
2. **Implement `retrieval-substitution-v1`** (the strategy ADR 009 actually proposes) and run the same gate. The current run only exercises naive truncation as the cheap baseline; the named ADR-009 strategy hasn't been measured.

Until both land, ADR 009 stands as written. The harness is operational and produced its first useful signal (recent-4 = regression). The verdict on the rest is pending more fixtures and the missing strategy.

### Prerequisites landed (2026-04-28, post-rescore)

Both pre-conditions for a meaningful re-run are now in place; the model run itself is pending.

**Depth-pressure fixtures (16–19).** Four new fixtures hit ≥17 real user turns each, with the load-bearing detail in turn 0 and 17–18 distractor turns between it and the assistant_target. Each fixture has a unit-test guard pinning two properties: (a) naive-truncate-16 actually drops the buried token from the assembled context, and (b) retrieval-substitution-v1 at recent-N=4 actually re-introduces the token via keyword overlap. Both must hold for the gate signal at this depth to be meaningful — without (a) the fixture isn't depth-pressure; without (b) any retrieval-vs-naive verdict reflects strategy mechanics rather than model behavior.

| # | Fixture | What it stresses | Anchor token |
|---|---|---|---|
| 16 | `deep-name-recall` | Identifier recall (codename) at depth-19 | `Albatross-9` |
| 17 | `deep-number-recall` | Numerical recall (£ amount) at depth-19 | `47,500` |
| 18 | `deep-decision-recall` | Tech-stack decision + rejected alternative | `PostgreSQL` |
| 19 | `deep-multi-detail-recall` | Two distinct facts (client + date) at depth-18 | `Northwind` |

**`RetrievalSubstitutionV1Strategy`.** Implementation lives at `backend/tests/eval/conversations/strategies.py`. The eval-side v1 reaches into the *dropped* portion of the conversation rather than into a vault — the conversation fixtures don't have a populated workspace, so the history is the corpus. Truncate to the last N user turns (identical to naive), score each dropped real-user-turn by content-token overlap with the latest user turn (deterministic stop-word filter, no external NLP), pick the top-K dropped pairs, prepend them as a synthesized user-role block in chronological order. Production's vault-backed equivalent is the next iteration; this isolates the retrieval-substitution variable from the workspace-population variable.

**Updated default sweep.** `python -m tests.eval.conversations.run_eval` now sweeps `full-history`, `naive-truncate-{4,8,12,16}`, and `retrieval-substitution-v1-{n8-k3, n4-k3}` by default. The CLI emits two gate-decision families: full-history-vs-naive (was already there) and naive-vs-retrieval at matched N (new). The retrieval-vs-naive comparison at N=4 and N=8 is the load-bearing test for ADR 009's preferred strategy.

**Run not yet executed.** The grid (7 strategies × 19 fixtures × 3 seeds = 399 calls) has not been launched against 30B-A3B yet — that's a user-triggered overnight run. The harness is operational; results pending.

## Second baseline run (2026-04-28 evening, post-Issue-4 swap) — decisive gate verdict

After the canonical-chat-model swap from `qwen3:30b-a3b` to `qwen3:14b` (Issue 4), the full grid was re-run against 14B with the default strategy sweep. This is the canonical baseline-0 for the conversation eval going forward. The artifact lives at `tests/eval/conversations/baselines/run-20260428T112547Z.json`.

**Run config.** 7 strategies × 19 fixtures × 3 seeds = 399 calls. `qwen3:14b` Q4_K_M, `temperature: 0`, `num_ctx: 16384`, `think: false`, fixed seeds (1, 2, 3), pinned RNG seed (17) for the bootstrap CI. Wall-clock: ~30–60 min on M5 Pro 24 GB / Ollama 0.18.0.

**Headline.** ADR 009's retrieval-first stance is empirically validated. Naive truncation regresses against full-history at every aggressive window size (N=4, 8, 12 all fail; N=16 kisses zero). Retrieval-substitution at matched N closes the gap fully — `retrieval-substitution-v1-n8-k3` matches full-history; `retrieval-substitution-v1-n4-k3` lands ~5 pp behind.

The full gate-decision table and the build plan for production wiring live in [ADR 009 §"Gate verdict (2026-04-28 evening) — ADR 009 stands; production wiring justified"](009-context-overflow-compaction.md#gate-verdict-2026-04-28-evening--adr-009-stands-production-wiring-justified). Reproduced summary:

| Comparison | Δ (B − A) | 95% CI | Verdict |
|---|---:|---|---|
| `full-history` vs `naive-truncate-4`  | −0.526 | [−0.789, −0.263] | regression |
| `full-history` vs `naive-truncate-8`  | −0.263 | [−0.474, −0.105] | regression |
| `full-history` vs `naive-truncate-12` | −0.211 | [−0.421, −0.053] | regression |
| `full-history` vs `naive-truncate-16` | −0.158 | [−0.316, +0.000] | equivalent (CI kisses zero) |
| `naive-truncate-4` vs `retrieval-substitution-v1-n4-k3` | +0.474 | [+0.263, +0.684] | **improvement** |
| `naive-truncate-8` vs `retrieval-substitution-v1-n8-k3` | +0.263 | [+0.105, +0.474] | **improvement** |

**What this changes about ADR 010 itself.** Nothing — the harness did exactly what it was filed to do. The decision gate produced decisive signal; the bootstrap-CI floor distinguished real effects from noise; the pinned chat model + fixtures + seeds + RNG made the verdict reproducible. The harness graduates from "newly built" to "operational and trusted."

**Comparison to the first baseline run** (Issue 3, against the broken 30B-A3B):

| Aspect | First run (30B-A3B, broken) | Second run (14B, swapped) |
|---|---|---|
| `full-history` vs `naive-truncate-16` | equivalent (Δ=0.000) | equivalent (Δ=−0.158, CI kisses zero) |
| `full-history` vs `naive-truncate-8` | equivalent (Δ=−0.111) | **regression** (Δ=−0.263) |
| `full-history` vs `naive-truncate-4` | regression (Δ=−0.444) | regression (Δ=−0.526) |
| `retrieval-substitution-v1` measured | no | **yes** |

The second run is the one that actually answers ADR 009's question. The first run's "naive-truncate-8 looks equivalent" verdict was contaminated by chain-of-thought-leak noise that obscured the real signal; the corrected fixtures + clean 14B output make the regression visible.

**The first run's baselines are preserved as historical artifacts** documenting the pre-swap state. They are NOT the canonical baseline-0; the second run is.

## Open follow-ups (non-blocking)

1. **Fixture-schema versioning.** Add `schema_version` to fixture JSON and to baselines. Re-recording protocol when tool surface or expected-facts schema changes.
2. **Local-judge upgrade trigger.** Define the criteria for swapping the default hosted judge to a local model: (a) Tier C dev hardware in place; (b) hosted-judge cost or rate-limit becoming material at the harness's run cadence; (c) a customer-facing reason to claim "the eval is fully local" (none expected, but worth recording). Until at least one of those is true, hosted-default is correct.
3. **Per-vertical fixture shards.** Legal / engineering / medical workloads have different conversation patterns. Once vertical-specific behavior diverges, fixtures fork by vertical. Out of scope for v1 launch; the original ten are cross-vertical, and the depth-pressure additions tag a vertical (`legal`, `engineering`, `generic`) but are still cross-vertical in spirit.
4. **CI integration for the floor test.** Currently opt-in via env var. Once the reference workspace and judge model are reliably available on a dedicated eval runner, promote to required-on-merge for retrieval/compaction-affecting paths.
5. **Token-cost regression budget.** The floor test gates latency; an analogous gate on tokens-into-judge per run would catch eval-cost runaway. Add once we have a few full-grid runs to set a budget.
6. **Confabulation rate as a first-class metric.** `must_not_contain` already exists; aggregating into a per-strategy "confabulation rate" surface in the output JSON makes the negative signal as visible as the positive one.
