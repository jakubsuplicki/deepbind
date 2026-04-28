"""Latency benchmark harness (ADR 011).

Sibling to ``tests/eval/conversations/`` (ADR 010). Where the conversation
harness measures *answer quality* under different context strategies, this
harness measures *user-perceived latency* under different inference knobs.

Same discipline: committed baselines, opt-in pre-merge gate, the diff is the
regression review. Different metric — TTFT / decode-tps / end-to-end wall
clock instead of clean-pass rate.

Public entry points:
- ``run_bench`` CLI for capturing baselines.
- ``runner.run_grid`` for programmatic invocation.
- ``gate.compare_runs`` for diffing two baselines under bootstrap CI.
"""
