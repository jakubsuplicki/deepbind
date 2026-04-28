---
title: Latency baseline
status: active
type: concept
sources:
  - backend/tests/eval/latency/scenarios.py
  - backend/tests/eval/latency/harness.py
  - backend/tests/eval/latency/runner.py
  - backend/tests/eval/latency/gate.py
  - backend/tests/eval/latency/run_bench.py
  - backend/tests/eval/latency/test_latency_floor.py
last_updated: 2026-04-28
last_reviewed: 2026-04-28
---

# Latency baseline

## Summary

A measurement substrate that captures user-perceived chat latency on the actual ICP hardware, commits the numbers to git, and uses them as the regression gate for every subsequent optimization. Sibling to the [conversation eval](../../backend/tests/eval/conversations/) ([ADR 010](../architecture/decisions/010-conversation-replay-eval-harness.md)) — same discipline (committed JSON baselines, opt-in pre-merge gate, bootstrap-CI verdict) applied to a different axis (TTFT / decode throughput / total wall clock instead of answer-quality clean-pass rate).

Filed as [ADR 011](../architecture/decisions/011-latency-benchmark-harness.md). The concept doc here is the practical companion: how the harness fits together, how to run it, how the optimization loop works.

## Why this exists

The product wedge claims "fast enough to displace shadow ChatGPT." The competitor analysis ([deep-dive-competitor-landscape.md](../research/deep-dive-competitor-landscape.md) §"Shadow-competitor reality check") names the empirical incumbent — Cmd-Tab to ChatGPT at 50–78% of professional-services staff — and the wedge requires beating it on perceived latency or losing the speed-to-value comparison.

Without a measurement substrate every claim about speed is a guess; every optimization knob is tuned by intuition; "did this change improve responsiveness?" has no defensible answer. The harness flips this: each knob gets a CI-significant yes/no against a frozen baseline.

## How it fits

```
backend/tests/eval/latency/
  scenarios.py             # data: 5 default + 1 reference scenario
  harness.py               # streaming Ollama HTTP + Anthropic streaming
  runner.py                # orchestrate, aggregate, capture machine info
  gate.py                  # bootstrap CI on numeric metric diffs
  run_bench.py             # CLI
  test_*.py                # unit tests (28 at v1)
  test_latency_floor.py    # opt-in JARVIS_LATENCY_BENCH=1 regression gate
  baselines/               # committed JSONs per (machine, knob_stack)
```

### Scenarios

A scenario is a stable `(name, system_prompt, user_message, max_output_tokens)` tuple. Three categories:

- **synthetic** — fixed-shape isolation scenarios. Padding text is deterministic (numbered lines) so the same scenario produces byte-identical input across runs.
  - `warm-short` — TTFT floor on a warm model.
  - `prefill-4k` / `prefill-16k` — prefill stress at approximate input-token sizes.
  - `decode-throughput` — short input, long output; sustained tokens/sec.
- **fixture** — derived from realistic conversation shapes. v1 includes one (`chat-realistic-shallow`); follow-on chunks add more.
- **reference** — Anthropic Claude Sonnet 4.x; the explicit competitor benchmark. Skipped silently when `ANTHROPIC_API_KEY` is absent.

### Harness

`OllamaTimedClient` streams `/api/chat` with `stream: true`, captures the time-to-first-token from the first non-empty content delta, and reads `eval_count` / `eval_duration` from the `done` event for sustained decode throughput (the model's self-reported counter, more honest than wall-clock counting because it excludes HTTP + JSON overhead). Mirrors production by setting `temperature: 0`, the configured `seed`, `num_ctx: 32768`, and `think: false` (v1 canonical posture).

`AnthropicTimedClient` is the reference-scenario sibling. Lazy-imports the SDK; returns an error result when no API key is present so the runner can continue cleanly.

### Runner

Orchestrates `(model × scenario × seed)` cells sequentially with one warm-up run discarded per cell. Aggregates timed runs into `ScenarioStats` (p50/p95/mean/stdev for each metric). Errored runs (cell didn't complete) are excluded from metric aggregation but counted in `n_errors`.

`capture_machine_info()` snapshots the platform string, Apple Silicon brand string (via `sysctl`), RAM, Ollama version (`ollama --version`), and the active knob stack. Two baselines are only comparable when their machine info matches.

### Gate

`compare_metric()` runs a paired bootstrap CI on the difference between two metric series, returning one of four verdicts:
- `improvement` — CI excludes zero in the better direction
- `regression` — CI excludes zero in the worse direction
- `equivalent` — CI straddles zero; no significant effect
- `insufficient_data` — fewer than 3 paired cells available

Direction is per-metric (`LOWER_IS_BETTER` for TTFT/total_ms, `HIGHER_IS_BETTER` for decode_tps), so "improvement" carries the same human meaning regardless of metric polarity.

`compare_runs()` is the convenience wrapper: applies the standard metric set to every paired cell and returns one decision per metric.

## How to run it

### Capture a baseline

```bash
cd backend
.venv/bin/python -m tests.eval.latency.run_bench
```

That's the whole command. The CLI:
1. Detects machine info (model label, RAM, Ollama version).
2. Runs every `(model × scenario × seed)` cell sequentially.
3. Writes the baseline JSON to `tests/eval/latency/baselines/<machine>-<timestamp>.json`.
4. Prints a one-line-per-cell summary at the end.

Wall-clock estimates on M5 Pro 24 GB:
- `--scope nightly` (default): 45–90 min, dominated by 30B-A3B + prefill-16k.
- `--scope pr`: 5–10 min — `warm-short` + `chat-realistic-shallow` × `qwen3:30b-a3b` only.

### Pin a knob stack

```bash
.venv/bin/python -m tests.eval.latency.run_bench \
  --knob-stack flash_attention,kv_cache_q8 \
  --out tests/eval/latency/baselines/baseline-2-flash-kvq8.json
```

The `knob_stack` is recorded in `machine_info` — diffs across baselines surface which knob produced which effect.

### Skip the reference scenario

```bash
.venv/bin/python -m tests.eval.latency.run_bench --no-reference
```

Useful for offline runs or when no Anthropic key is configured.

### Run the floor gate locally

```bash
JARVIS_LATENCY_BENCH=1 .venv/bin/python -m pytest tests/eval/latency/test_latency_floor.py -v
```

Skips with a clear message until `baselines/baseline-0.json` exists. Once committed, verifies the baseline is structurally healthy (machine info present, all cells have the standard metric set, no unexplained errors).

## The optimization loop

```
   write code → run benchmark → diff vs last baseline
                                       │
                  ┌────────────────────┴────────────────────┐
                  ▼                                         ▼
         improvement (CI excludes 0)              equivalent / regression
                  │                                         │
       commit new baseline                       revert; document why
       knob is now in the stack                  in ADR 011's "rejected knobs"
```

In ADR-011 terms:

1. **baseline-0** is captured with `knob_stack=[]` — stock Ollama 0.18.0 on M5 Pro 24 GB.
2. For each knob (flash attention, prefix caching, KV-cache q8, speculative decoding, etc.):
   - Enable the knob, re-run `run_bench`.
   - Run `compare_runs` against baseline-N. (Future: a `compare_baselines.py` script automates this — open follow-up.)
   - If the bootstrap CI on the relevant metric excludes zero on the improvement side: commit a new baseline with the knob added to `knob_stack`. The knob is "in."
   - If `equivalent` or `regression`: revert the knob, document the result in ADR 011's open follow-ups, move to next knob.
3. **Anything else that touches latency** (chat router changes, retrieval pipeline changes, model bumps, Ollama bumps) is checked against the floor gate before merge.

## What invalidates a baseline

A baseline is anchored to its `machine_info`. Changes that invalidate it (require a new baseline, not just a comparison):

- Different machine.
- Ollama version bump.
- macOS major version bump.
- Model version bump (re-tagged weights).
- Quantization change.
- Scenario shape change (system prompt content, padding size, max output tokens).
- Schema change (`schema_version` bump).

Knob-stack changes are *not* invalidating — that's the whole point; they're how new baselines are produced.

## What it explicitly does not do

- **Cold-start measurement.** Requires Ollama process control (kill + restart between runs). Deferred to a follow-on chunk where it's actually needed by a knob being measured.
- **System counters** (powermetrics on macOS, GPU utilization, memory bandwidth saturation). High diagnostic value, but adds complexity. Open follow-up.
- **Cross-machine matrix.** Single machine first (M5 Pro 24 GB). Windows + RTX, additional Macs, MLX-direct are each their own chunks against the schema established here.
- **Continuous benchmarking infrastructure.** No automated runs on every commit; the harness runs locally. Promotion to required-on-merge in CI is gated on dedicated eval hardware.
- **Production-path changes.** This is dev infra; nothing customer-facing imports from `tests/eval/latency/`.

## Cross-references

- **[`docs/concepts/eval-baseline.md`](eval-baseline.md)** — sibling concept doc for the retrieval-quality baseline. Same discipline.
- **[`backend/tests/eval/conversations/`](../../backend/tests/eval/conversations/)** — sibling harness for answer-quality measurement. Different metric, same shape.
- **[ADR 011](../architecture/decisions/011-latency-benchmark-harness.md)** — the architectural decision; this concept doc is the practical companion.
- **[`docs/research/product-direction-v1-v2.md`](../research/product-direction-v1-v2.md) §11.3** — competitor analysis that motivates the latency budget.
- **[`docs/features/local-models.md`](../features/local-models.md)** — the surface the harness measures.
- **[`docs/features/chat.md`](../features/chat.md)** — the chat router whose performance is gated.
