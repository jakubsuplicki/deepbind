# ADR 011 — Latency benchmark harness for measurement-driven optimization

**Status:** Accepted
**Date:** 2026-04-28
**Related:** [ADR 010](010-conversation-replay-eval-harness.md) · [ADR 009](009-context-overflow-compaction.md) · [`docs/concepts/latency-baseline.md`](../../concepts/latency-baseline.md)

## Context

The product wedge is "fast enough to displace shadow ChatGPT and DIY Ollama." The competitor analysis is explicit on this — Cmd-Tab to ChatGPT is the empirical incumbent at 50–78% of professional-services staff, and the voice/chat replacement must beat it on perceived latency or it loses the speed-to-value comparison. Despite this, no commitment to a latency budget exists in the codebase. Every claim about speed is currently a guess.

Without a measurement substrate, optimization knobs (flash attention, prefix caching, speculative decoding, KV-cache quantization, MLX-direct) are tuned by intuition — and the question "did this change improve responsiveness?" has no defensible answer. The same trap ADR 010 was filed to prevent, applied to a different axis: *write the harness first, freeze the baseline, swap one knob behind it, diff*.

This ADR commits to a sibling of [ADR 010](010-conversation-replay-eval-harness.md)'s discipline — committed JSON baselines, opt-in pre-merge gate, bootstrap-CI verdict — applied to user-perceived latency on the actual ICP hardware (M-series Macs at the floor; Windows + RTX in a follow-on chunk).

## Decision drivers

1. **Reproducibility on end-user hardware.** Numbers must be machine-anchored. M5 Pro 24 GB measurements are different artifacts from M4 Pro 24 GB or Windows + RTX 4090. Baselines are per-`(machine, model, knob_stack)`, not global.
2. **Mirrors production exactly.** Ollama HTTP `chat` endpoint with `stream: true`, the canonical chat model loadout, the same `think: false` posture v1 ships with. What benchmark callers see is what shipped users experience.
3. **Determinism for regression detection.** Temperature 0, fixed seed, fixed input → same scenario + same machine + same knob stack → comparable run.
4. **Bootstrap CI on diffs, not fixed-percent tolerance.** The conversations harness already established the pattern in [`gate.py`](../../../backend/tests/eval/conversations/gate.py): paired bootstrap CI, verdict is `improvement` / `regression` / `equivalent` / `insufficient_data`. Fixed-percent thresholds are sloppy at small sample sizes; a CI that excludes zero is honest.
5. **Explicit competitor reference.** The wedge's claim is "faster than the cloud option people Cmd-Tab to." The harness ships a reference scenario that hits Anthropic's hosted streaming API with the same prompt as `warm-short`, so the comparison is a recorded number, not a slogan.
6. **Sibling to existing eval discipline.** Same shape as `step-28c.json` + `test_baseline_floor.py`: committed JSON, sorted keys, opt-in env-var gate. `git diff` of `tests/eval/latency/baselines/` is the regression review.
7. **No premature surface.** Single-machine first (M5 Pro 24 GB). Matrix (Windows + RTX, more Macs, MLX experiments) is a follow-on chunk that adds machines, not redesigns the schema.
8. **Two-tier scope.** Full grid (`--scope nightly`, ~45–90 min on M5 Pro 24 GB) for canonical baselines; subset (`--scope pr`, ~5–10 min) for fast PR checks and the floor gate.

## Decision

### Layout

A new sibling subtree under `backend/tests/eval/`, mirroring `conversations/`:

```
backend/tests/eval/latency/
  __init__.py
  scenarios.py             # synthetic + fixture-derived + reference scenarios
  harness.py               # streaming Ollama HTTP + Anthropic reference
  runner.py                # orchestrate, aggregate, capture machine info
  gate.py                  # bootstrap CI on numeric metric diffs
  run_bench.py             # CLI: python -m tests.eval.latency.run_bench
  test_*.py                # unit tests (28 at v1 launch)
  test_latency_floor.py    # opt-in JARVIS_LATENCY_BENCH=1 regression gate
  baselines/               # committed JSONs per (machine, knob_stack)
```

### Scenarios (v1 set)

Five scenarios cover the four phases that matter for user-perceived latency, plus a competitor reference:

| Name | Category | What it stresses |
|---|---|---|
| `warm-short` | synthetic | TTFT floor on a warm model — the "hello, summarize this" everyday case |
| `prefill-4k` | synthetic | Medium-context prefill throughput |
| `prefill-16k` | synthetic | Long-context prefill — what 30 turns deep into a conversation feels like |
| `decode-throughput` | synthetic | Sustained decode tokens/sec on a long output |
| `chat-realistic-shallow` | fixture | Realistic 25-turn-deep conversation shape derived from fixture #1 |
| `reference-anthropic-warm-short` | reference | Same as `warm-short`, against Anthropic Claude Sonnet 4.x for the explicit competitor benchmark |

Cold-start is omitted at v1 — it requires Ollama process control that's better handled in a follow-on chunk. Per-fixture latency from the conversation eval is implicitly covered by re-running that harness; the latency harness itself uses one representative fixture-derived scenario to keep its own grid bounded.

### Models (v1 default)

- `qwen3:8b` (Q4_K_M) — hardware-floor model
- `qwen3:14b` (Q4_K_M) — comfortable-mid model
- `qwen3:30b-a3b` (Q4_K_M) — v1 canonical chat model (MoE, 3B active)

Per-call: 5 timed runs after 1 discarded warm-up. Sequential execution; parallel runs would compete for memory/GPU and pollute the numbers.

### Metrics captured per cell

Per `(model, scenario)`:
- `ttft_ms_p50`, `ttft_ms_p95`, `ttft_ms_mean`, `ttft_ms_stdev` — time from request send to first non-empty content delta. The "feels fast" metric.
- `decode_tps_p50/p95/mean/stdev` — sustained decode throughput; computed from Ollama's reported `eval_count / eval_duration` (excludes HTTP + JSON parse overhead).
- `total_ms_p50/p95/mean/stdev` — end-to-end wall clock.
- `output_tokens_mean`, `prompt_tokens_mean` — for sanity checks across runs.

### Output (stable-key JSON)

Mirrors `step-28c.json` discipline. Stable schema:

```jsonc
{
  "schema_version": 1,
  "run_metadata": {
    "timestamp_utc": "2026-04-28T...",
    "n_warmup_runs": 1, "n_timed_runs": 5,
    "seeds": [1, 2, 3, 4, 5]
  },
  "machine_info": {
    "model_label": "Apple M5 Pro",
    "ollama_version": "ollama version is 0.18.0",
    "platform": "darwin-arm64",
    "ram_gb": 24,
    "knob_stack": []
  },
  "by_cell": [
    {
      "model_id": "ollama:qwen3:30b-a3b",
      "scenario_name": "warm-short",
      "ttft_ms_p50": 1200.0, "ttft_ms_p95": 1280.0,
      "decode_tps_p50": 28.4, "decode_tps_p95": 27.1,
      "total_ms_p50": 2100.0, "total_ms_p95": 2350.0,
      "output_tokens_mean": 4.0, "prompt_tokens_mean": 28.0,
      "n_timed_runs": 5, "n_errors": 0, "errors": []
    }
  ]
}
```

### Bootstrap-CI gate

`gate.compare_metric()` takes two paired float series (matched by `(model_id, scenario_name)` across two baselines) and returns a `GateDecision` with one of four verdicts (`improvement` / `regression` / `equivalent` / `insufficient_data`). Direction is per-metric: `LOWER_IS_BETTER` for TTFT/total_ms, `HIGHER_IS_BETTER` for decode_tps. The CI is paired-by-cell so cell-level variance is preserved.

`compare_runs()` is the convenience wrapper: applies the standard metric set across every cell pair, returns one decision per metric. Used by future `compare_baselines` tooling.

### Pre-merge gate

`test_latency_floor.py` activates only when `JARVIS_LATENCY_BENCH=1` is set. Even then, it skips silently when no canonical baseline exists yet. Once a `baselines/baseline-0.json` is committed, the test verifies machine info is recorded, every cell has the standard metric set, and no non-reference cells errored out.

This is a pre-merge gate run *locally* before merging chat / retrieval / local-models changes. Promotion to required-on-merge in CI is gated on dedicated eval hardware existing — the same path ADR 010 takes.

### What invalidates a baseline

A baseline is anchored to its `machine_info`. The following changes invalidate it (require capturing a new baseline before continuing the optimization loop):

- Different machine (different `model_label`, `ram_gb`).
- Ollama version bump.
- macOS major version bump.
- Model version bump (e.g., `qwen3:30b-a3b` re-tagged with new weights).
- Quantization change (Q4_K_M → IQ4_XS).
- Any change to scenario shape (system prompt content, padding size, max output tokens).
- Schema change to baseline JSON (`schema_version` bump).

Knob-stack changes are *not* invalidating — that's the whole point; they're how new baselines are produced. A baseline-N captured with `knob_stack=[flash_attention]` is comparable against baseline-(N-1) with `knob_stack=[]` *on the same machine*.

## Alternatives considered

### A. Off-the-shelf benchmark tools (`llama-bench`, `optimum-benchmark`, `genai-perf`)
Rejected. They benchmark raw model throughput with synthetic inputs; user-perceived latency is end-to-end through Ollama HTTP with the actual production prompt shape. Off-the-shelf numbers don't predict the metric we care about.

### B. Bolt latency onto the conversations harness
Rejected. Conversations measure answer quality at fixed temperature 0; latency measures time across context shapes. Bolting them together conflates two evaluations and makes both harder to keep stable. Sibling subdirectory keeps both baselines independent.

### C. Skip the harness; trust ad-hoc `curl` and intuition
Rejected for the same reason ADR 010 was filed: numbers stop being credible the moment the developer has a stake in their direction. Freeze the baseline, swap one knob behind it, diff.

### D. CI on cloud GPU
Rejected. The whole point is measuring on the end-user hardware tier. Cloud GPU tells us nothing about M-series Macs.

### E. Reuse `OllamaChat` adapter from conversations
Rejected. That adapter returns the complete response — wrong abstraction for TTFT measurement. The latency harness needs per-token timestamps from the streaming endpoint. Different concern, separate client.

### F. Fixed-percent regression tolerance instead of bootstrap CI
Rejected. "10% tolerance" is sloppy at small sample sizes. Bootstrap CI is the same tool the conversations harness already uses — same statistical floor for both.

## Consequences

### Positive
- The "are we faster than shadow ChatGPT?" claim becomes a recorded number, not a slogan.
- Optimization knob loop is honest: each knob gets a CI-significant yes/no, not "feels faster."
- Future regressions in chat / retrieval / local-models are caught by the floor gate before merge.
- Cross-cell visibility into where time goes (prefill at 16k vs decode-throughput vs warm-short) makes follow-on optimization choices evidence-based.

### Negative
- Wall-clock cost of a full grid (~45–90 min on M5 Pro 24 GB). Mitigated by `--scope pr` for PR checks; full grid only on knob changes and pre-release.
- Adds a new subtree to maintain. Worth it given the discipline payoff; mitigated by mirroring the conversations harness shape (familiar pattern).
- Ollama-version-pinning (0.18.0 on Apple M5 + macOS 26) is a real constraint — see ADR 010's footnote on the v0.21.x crash. Documented in `machine_info` so a future Ollama bump that changes throughput is visible.
- Reference scenario depends on Anthropic API availability + key. Mitigated: the harness skips the reference scenario silently when no key is present, and the floor test treats reference-only errors as acceptable.

### What this changes about existing code

- New subtree `backend/tests/eval/latency/` (no changes to existing modules).
- New ADR 011, new concept doc `docs/concepts/latency-baseline.md`.
- Registry gains a new feature entry `latency-benchmark`; cross-links from `chat.md` and `local-models.md`.
- No production-path changes. The harness is dev infra; nothing user-facing imports from it.

### What this does NOT change
- Production chat / retrieval / local-models behavior is unchanged.
- The conversations harness is unchanged.
- ADR 003 (Tauri bundling), ADR 009 (context compaction), ADR 010 (conversation eval) are unaffected. The latency harness gives them measurable signal but doesn't depend on any of them.

## Migration path

1. **Substrate lands** (this ADR's chunk): code, ADR, concept doc, registry entry, opt-in floor test. **No knobs turned.** ✅
2. **Capture baseline-0** on M5 Pro 24 GB. One command: `python -m tests.eval.latency.run_bench`. ~45–90 min. Output committed to `baselines/`.
3. **Knob loop**, one chunk per knob, against baseline-0:
   - Verify prefix caching is enabled (`cache_prompt: true`)
   - `OLLAMA_FLASH_ATTENTION=1`
   - `OLLAMA_KV_CACHE_TYPE=q8_0`
   - Verify ingest-time embedding (no query-time re-embed)
   - Speculative decoding with `qwen3:1.7b` as draft
   - Hardware-fit auto-pick at install
4. **Floor test goes from skip to required-on-merge** for chat/retrieval/local-models paths once dedicated eval hardware exists.
5. **Matrix expansion** — Windows + RTX, additional Macs, MLX-direct experiment — each its own chunk against the schema established here.

## Amendment 1 (2026-04-28 evening) — first-PR-scope-run findings + canonical chat model swap

The first PR-scope run on M5 Pro 24 GB against `qwen3:30b-a3b` produced numbers that, on inspection, were measuring a broken model setup rather than honest inference throughput:

| Scenario | TTFT p50 | TPS p50 | total p95 |
|---|---:|---:|---:|
| warm-short | 220 ms | 16.1 | 1.2 s |
| chat-realistic-shallow | 590 ms | 5.5 | **14.6 s** |

A direct curl against the same model with `num_predict: 8` returned `"Okay, the user asked me to say"` — pure chain-of-thought. `think: false` was not being honored on Ollama 0.18.0 for `qwen3:30b-a3b`. See [ADR 010 Issue 4](010-conversation-replay-eval-harness.md#issue-4-2026-04-28-evening--qwen3-30b-a3b-thinkfalse-leak-canonical-chat-model-swap-to-qwen3-14b) for the full reproduction.

Re-running the same PR scope against `qwen3:14b` (which honors `think: false`) produced:

| Scenario | TTFT p50 | TPS p50 | total p95 |
|---|---:|---:|---:|
| warm-short | 150 ms | 20.3 | 299 ms |
| chat-realistic-shallow | 150 ms | 14.0 | **1.8 s** |

The chat-realistic improvement is 8×; the harness was never the issue, the model + Ollama-version combination was.

### Decision changes

1. **Canonical model for nightly grid is `qwen3:14b`** until ADR 012's self-test lands. `DEFAULT_MODELS_NIGHTLY` and `DEFAULT_MODELS_PR` updated accordingly.
2. **`qwen3:30b-a3b` is excluded from the default model list** on this Ollama version. It can still be benchmarked explicitly (`--models qwen3:30b-a3b`) for diagnostic purposes, but its numbers should not be promoted to a canonical baseline.
3. **Per-machine canonical-model selection moves to [ADR 012](012-chat-model-self-test.md)** — the self-test that runs on the user's machine, probes correctness + fit + speed, and picks the best model for that environment. The latency harness grows a new role beyond dev infra: its scenario set + harness module become the runtime probe's measurement substrate.

### Baseline-0 status

The captured `apple-m5-20260428T084332Z.json` (against `qwen3:30b-a3b`) is **preserved as a historical artifact** documenting the broken-model state, not promoted as the canonical baseline-0. The next nightly run against `qwen3:14b` is the canonical baseline-0 going forward; all subsequent knob-loop diffs reference it.

## Amendment 2 (2026-04-28 evening) — harness diagnostic gaps + reference-scenario duplication fix

The first 14B-pinned grid surfaced three small harness gaps the original substrate didn't anticipate:

1. **Reference scenarios duplicated per Ollama model.** The original `run_grid` looped `for model in models: for scenario in scenarios:`, which ran the Anthropic reference scenario *once per Ollama model in the grid* (5 errors × 2 = 10 misleading rows in the baseline JSON). Reference scenarios are provider-independent; running them per-model inflates the wall clock and pollutes the JSON. **Fix:** split scenarios into `ollama_scenarios` and `reference_scenarios` inside `run_grid`; reference scenarios run **once per grid invocation**, not per model.

2. **Models not pulled in Ollama errored 5× per scenario instead of skipping cleanly.** A user running the grid against `qwen3:8b` without having pulled it got 25 hard HTTP-404 errors recorded in the baseline. **Fix:** added `_probe_pulled_models(base_url)` that hits `GET /api/tags` once at run start; absent models record a single sentinel `ScenarioStats` per scenario with `skip_reason` set ("model 'qwen3:8b' not pulled in Ollama"), no failed runs. Floor test allows skipped cells; non-skipped cells with errors still fail unless they're reference scenarios.

3. **No sample response text in the baseline JSON.** The first investigation into "why is output_tokens exactly at the cap?" had to fall back to `curl` because `response_text` was captured per run but not serialized. **Fix:** `aggregate()` now plucks the first non-errored run's response text (truncated to 500 chars) into a new `sample_response_text` field on `ScenarioStats`, surfaced as a top-level cell field in the baseline JSON. A future thinking-leak / wrong-shape-output investigation can read the JSON instead of re-running the model.

### Schema changes

`ScenarioStats` and the per-cell baseline JSON gained two fields:

- `skip_reason: Optional[str]` — non-null when the cell was skipped (typically because the model isn't pulled).
- `sample_response_text: str` — first non-errored run's response text, truncated to 500 chars; empty string when all runs errored.

Existing baselines remain readable (the new fields are optional). The schema_version remains 1; these additions are additive, not breaking.

### Tests added

- `test_run_grid_skips_models_not_pulled_in_ollama` — pulled-models prefilter.
- `test_run_grid_runs_reference_scenarios_only_once_per_grid` — duplicate-reference regression test.
- `test_aggregate_captures_sample_response_text` + `test_aggregate_truncates_long_response_text` — sample text capture + truncation.

## Amendment 3 (2026-04-28 evening) — canonical baseline-0 captured against Qwen3-14B

After Amendment 1's chat-model swap and Amendment 2's harness fixes, the canonical baseline-0 was captured. Artifact: `tests/eval/latency/baselines/apple-m5-20260428T102349Z.json`. This is the reference point against which all future optimization knobs diff.

**Run config.** Nightly scope (default). `qwen3:14b` (qwen3:8b skipped cleanly because not pulled — the Amendment 2 skip behavior worked as designed). 5 scenarios × 5 timed runs × 1 model × 5 seeds = 25 timed cells. `--no-reference` (no Anthropic key set; the Amendment 1 reference scenario is informational, not load-bearing for tonight's run). Wall-clock: ~25 min.

### Baseline-0 numbers on M5 Pro 24 GB / Ollama 0.18.0 / Qwen3-14B Q4_K_M / stock knobs

| Scenario | TTFT p50 | TPS p50 | total p95 | Sample response (verbatim) |
|---|---:|---:|---:|---|
| `warm-short` | 151 ms | 20.3 | 310 ms | `"Hi."` |
| `prefill-4k` | 163 ms | 14.5 | 732 ms | `"The capital of France is Paris."` |
| `prefill-16k` | 194 ms | 12.1 | 869 ms | `"The capital of France is Paris."` |
| `chat-realistic-shallow` | 154 ms | 14.0 | 1809 ms | `"You mentioned earlier that you were work…"` (correctly recalled the topic-defining detail from the synthesized history) |
| `decode-throughput` | 157 ms | 13.5 | 21643 ms | `"The periodic table is a systematic arran…"` (real content) |

**Three findings.**

1. **Sample responses confirm correctness.** The Amendment 2 `sample_response_text` field paid for itself on this single run — every cell shows a real answer, not chain-of-thought prose. The chat-realistic scenario's response actually quotes the topic the model was supposed to recall, validating that 14B + `think: false` + the realistic-conversation shape produces correct output. This is the visible difference from the broken 30B-A3B run earlier in the day.

2. **Prefill is a gentle slope, not a cliff.** Going from warm-short (effectively 0K context) to prefill-16k (16K context):
   - TTFT: 151 ms → 194 ms (+28%) — roughly linear in context.
   - TPS: 20.3 → 12.1 (−40%) — meaningful but not catastrophic. The 30B-A3B equivalent dropped 58%.
   - total p95: 310 ms → 869 ms (+180%) — but at 16K context the absolute number is still under 1 second.

   This means long-conversation behavior is *usable* on 14B even before compaction lands; with ADR 009's retrieval-first compaction wired (gate-validated tonight), the prefill-16k case reduces to the prefill-4k case for typical long-conversation usage.

3. **Numbers are reproducible.** PR-scope earlier in the day captured warm-short at 154 ms / 20.3 TPS / 311 ms p95; the full nightly captured 151 / 20.3 / 310 — within ~1% noise. Run-to-run repeatability is well inside the bootstrap-CI tolerance, which means future knob diffs won't be drowned in noise.

### What baseline-0 unlocks

The optimization knob loop becomes runnable. Each subsequent `run_bench --knob-stack <name>` can be diffed against this baseline via `gate.compare_runs()`; the bootstrap CI returns improvement / regression / equivalent / insufficient_data.

Highest-priority knobs to try, in order:

1. **Speculative decoding** with `qwen3:1.7b` as draft model. Already pulled. Expected: +50–100% TPS lift across scenarios. If TPS jumps from 14 → 25 on chat-realistic, total p95 drops from 1.8 s to ~1 s.
2. **`OLLAMA_FLASH_ATTENTION=1`**. One env var, modest gain, free to verify.
3. **`OLLAMA_KV_CACHE_TYPE=q8_0`**. Halves KV memory; useful headroom on 24 GB. Slight quality cost — measure on the conversation eval simultaneously to verify no clean-pass regression.
4. **Prefix caching verification.** Multi-turn conversations should reuse the system-prompt prefill across turns. Verify Ollama is doing this; if not, configure.

Each knob is its own chunk: enable, re-run nightly (~25 min), commit baseline-N if CI excludes zero.

### Skipped cells in baseline-0

`qwen3:8b` cells show `SKIP` with `skip_reason: "model 'qwen3:8b' not pulled in Ollama"`. The Amendment 2 prefilter caught this at run start; the cells are recorded as informational gaps. To fill them in: `ollama pull qwen3:8b` and re-run. The 8B numbers become load-bearing once the install-time chat-model probe (ADR 012) needs them for hardware-floor recommendation logic.

## Open follow-ups (non-blocking)

1. **Cold-start scenario.** Requires Ollama process control (kill + restart between runs). Add when the first knob-loop chunk needs it.
2. **System counters during runs** (powermetrics on macOS, perf counters on Windows). Diagnostic value: explains *why* a knob helped, not just that it did.
3. **CV check + automatic retry on noisy runs.** When standard deviation exceeds a threshold, redo the run; flag if it persists.
4. **Knob-stack as a versioned enum** — prevent `flash_attention=true` and `flash-attn` accidentally being different baselines. Add when more than 2–3 knobs are in active use.
5. **`compare_baselines.py` script** — load two baseline JSONs, pair cells, run `gate.compare_runs`, emit the diff report. Add when the second baseline lands.
6. **Per-vertical fixture shards.** Once vertical-specific latency patterns diverge (long legal-vertical conversations vs short engineering-vertical Q&A), fork the fixture-derived scenarios.
