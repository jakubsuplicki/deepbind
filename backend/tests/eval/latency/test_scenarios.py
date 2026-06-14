"""Unit tests for the scenario definitions (ADR 011)."""

from __future__ import annotations

from tests.eval.latency.scenarios import (
    CHARS_PER_TOKEN_APPROX,
    ScenarioCategory,
    chat_realistic,
    decode_throughput,
    default_scenarios,
    prefill_scenario,
    warm_short,
)


def test_warm_short_is_minimal():
    s = warm_short()
    assert s.category is ScenarioCategory.SYNTHETIC
    assert len(s.user_message) < 50
    assert s.max_output_tokens <= 16


def test_prefill_scenario_grows_with_target_tokens():
    small = prefill_scenario(1_000)
    big = prefill_scenario(16_000)
    assert len(big.system_prompt) > len(small.system_prompt) * 8
    # The padding should approximately match target * chars-per-token
    assert len(big.system_prompt) >= 16_000 * CHARS_PER_TOKEN_APPROX * 0.9


def test_prefill_scenario_is_deterministic():
    a = prefill_scenario(4_000)
    b = prefill_scenario(4_000)
    assert a.system_prompt == b.system_prompt
    assert a.user_message == b.user_message


def test_default_scenarios_covers_phases():
    """The default set must cover prefill, decode, realistic chat, and warm baseline."""
    names = {s.name for s in default_scenarios()}
    assert "warm-short" in names
    assert "prefill-4k" in names
    assert "prefill-16k" in names
    assert "decode-throughput" in names
    assert "chat-realistic-shallow" in names


def test_default_scenarios_has_no_hosted_reference_scenario():
    """Local-only build (ADR 015): no hosted-API reference scenario exists."""
    names = {s.name for s in default_scenarios()}
    assert not any("reference" in n for n in names)
    assert not any("anthropic" in n for n in names)


def test_decode_throughput_is_long_output():
    s = decode_throughput()
    assert s.max_output_tokens >= 256
    assert s.category is ScenarioCategory.SYNTHETIC


def test_chat_realistic_is_fixture_category():
    s = chat_realistic()
    assert s.category is ScenarioCategory.FIXTURE


def test_approx_input_tokens_is_proportional_to_size():
    short = warm_short()
    big = prefill_scenario(16_000)
    assert big.approx_input_tokens() > short.approx_input_tokens() * 100
