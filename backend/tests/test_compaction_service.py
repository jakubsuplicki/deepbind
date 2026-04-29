"""Tests for the production-side compaction service (ADR 009).

These tests pin the contract that the chat router relies on:

- Under-threshold conversations are not compacted; the original message
  list is returned with a token-count snapshot. This is the path that
  fires on every short turn — a regression here means every turn pays
  for retrieval.
- Over-threshold conversations drop older real-user turns down to the
  configured recent_n boundary, regardless of how many tool_result
  user-role messages are interleaved (those don't count toward N).
- Vault retrieval, when it returns matches, prepends a synthesized
  user-role substitution block and never mutates the kept window.
- Vault retrieval failures degrade gracefully — compaction proceeds
  without substitution rather than failing the turn.
- ``<think>`` scratchpad is stripped from assistant turns regardless of
  whether the compaction trigger fires.
- Audit-event payload (``CompactionResult.as_event``) carries the
  fields ADR 009 §"Audit trail" pins.
"""

from __future__ import annotations

import os

# Same offline guard as test_token_counting — the compaction service
# imports token_counting which would otherwise try the HF cache on the
# tokenizer-present path.
os.environ.setdefault("HF_HUB_OFFLINE", "1")

import pytest

from services.compaction_service import (
    DEFAULT_RECENT_N,
    DEFAULT_THRESHOLD_PCT,
    DEFAULT_TOP_K,
    CompactionResult,
    compact_messages,
)


pytestmark = pytest.mark.anyio(backends=["asyncio"])


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


# ── Fixtures ──────────────────────────────────────────────────────────


def _make_history(num_user_turns: int, body_chars: int = 40) -> list[dict]:
    """Build a synthetic alternating user/assistant history."""
    history: list[dict] = []
    for i in range(num_user_turns):
        history.append({"role": "user", "content": ("u%d " % i) * (body_chars // 4)})
        history.append({"role": "assistant", "content": ("a%d " % i) * (body_chars // 4)})
    return history


# ── Trigger / threshold behavior ──────────────────────────────────────


async def test_under_threshold_returns_uncompacted():
    history = _make_history(num_user_turns=3, body_chars=20)
    result = await compact_messages(
        history,
        effective_context_tokens=32_768,
        system_prompt_tokens=200,
    )
    assert result.compacted is False
    assert result.reason == "under_threshold"
    assert result.messages == history  # unchanged content
    assert result.tokens_before == result.tokens_after
    assert result.recent_window_size == 3
    assert result.threshold_pct == DEFAULT_THRESHOLD_PCT


async def test_over_threshold_drops_to_recent_n_boundary(monkeypatch):
    # Skip the vault retrieval entirely so the test isolates
    # the cut-index behaviour from the substitution behaviour.
    async def _no_vault_results(*args, **kwargs):
        return []

    monkeypatch.setattr(
        "services.retrieval.sessions.find_earlier_turn_context",
        _no_vault_results,
    )

    history = _make_history(num_user_turns=20, body_chars=200)
    # Tiny effective ceiling forces the trigger.
    result = await compact_messages(
        history,
        effective_context_tokens=400,
        system_prompt_tokens=50,
        output_reserve_tokens=50,
        recent_n=4,
        top_k=0,  # disable substitution to keep this test focused
    )

    assert result.compacted is True
    assert result.reason == "compacted"
    assert result.recent_window_size == 4
    assert result.turns_dropped == 16
    # 4 user turns × 2 messages = 8 messages kept
    assert len(result.messages) == 8
    # First kept message must be a real user turn — preserves the
    # tool_use/tool_result pairing contract for downstream providers.
    assert result.messages[0]["role"] == "user"


async def test_recent_n_floor_protects_minimum_window(monkeypatch):
    """Asking for recent_n=1 must clamp up to the floor (2)."""
    async def _no_vault_results(*args, **kwargs):
        return []

    monkeypatch.setattr(
        "services.retrieval.sessions.find_earlier_turn_context",
        _no_vault_results,
    )

    history = _make_history(num_user_turns=20, body_chars=100)
    result = await compact_messages(
        history,
        effective_context_tokens=400,
        system_prompt_tokens=50,
        output_reserve_tokens=50,
        recent_n=1,
        top_k=0,
    )
    assert result.recent_window_size == 2  # floor applied


async def test_compaction_skipped_when_history_already_within_recent_n():
    history = _make_history(num_user_turns=4, body_chars=4000)  # very long messages
    result = await compact_messages(
        history,
        effective_context_tokens=400,
        system_prompt_tokens=50,
        output_reserve_tokens=50,
        recent_n=8,
        top_k=0,
    )
    # History is over budget but only 4 user turns exist — strategy
    # can't help; surfaces the diagnostic reason.
    assert result.compacted is False
    assert result.reason == "recent_window_already_minimal"
    assert result.recent_window_size == 4


async def test_zero_or_negative_budget_skipped():
    history = _make_history(num_user_turns=10, body_chars=10)
    result = await compact_messages(
        history,
        effective_context_tokens=100,
        system_prompt_tokens=200,  # already over the ceiling
        output_reserve_tokens=4096,
    )
    assert result.compacted is False
    assert result.reason == "budget_too_small"


# ── Recent-N counting on tool-loop interleaved histories ──────────────


async def test_tool_result_messages_do_not_inflate_recent_n(monkeypatch):
    """User-role messages whose content is purely tool_result are not
    counted as user turns. A history with real user turns and a long
    tool-loop in between must still keep the right number of *real* turns.
    """
    async def _no_vault_results(*args, **kwargs):
        return []

    monkeypatch.setattr(
        "services.retrieval.sessions.find_earlier_turn_context",
        _no_vault_results,
    )

    real_turns = 10
    history: list[dict] = []
    for i in range(real_turns):
        history.append({"role": "user", "content": "real user turn " + "x" * 200})
        # Insert a tool-loop pair so user-content counting has noise.
        history.append({
            "role": "assistant",
            "content": [{"type": "tool_use", "id": f"t{i}", "name": "read_note", "input": {"path": "x"}}],
        })
        history.append({
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": "y" * 200}],
        })
        history.append({"role": "assistant", "content": "assistant reply " + "z" * 200})

    result = await compact_messages(
        history,
        effective_context_tokens=400,
        system_prompt_tokens=20,
        output_reserve_tokens=50,
        recent_n=3,
        top_k=0,
    )
    assert result.compacted is True
    # Exactly 3 real user turns kept (counting tool_result-only user
    # messages would have falsely satisfied recent_n with 1 real turn).
    real_user_count = sum(
        1 for m in result.messages
        if m.get("role") == "user" and isinstance(m.get("content"), str)
    )
    assert real_user_count == 3
    assert result.recent_window_size == 3


# ── Vault substitution ────────────────────────────────────────────────


async def test_vault_results_become_synthesized_prefix(monkeypatch):
    async def _fake_vault(query: str, *, current_session_id: str = "", top_k: int = 3, **_):
        assert top_k == 2
        # Echo back something inspectable so the test can assert the
        # synthesized block actually carries the vault content.
        return [
            {"path": "conversations/2026-04-01-x.md", "title": "Earlier chat A", "snippet": "Old context A"},
            {"path": "conversations/2026-04-02-y.md", "title": "Earlier chat B", "snippet": "Old context B"},
        ]

    monkeypatch.setattr(
        "services.retrieval.sessions.find_earlier_turn_context",
        _fake_vault,
    )

    history = _make_history(num_user_turns=10, body_chars=200)
    result = await compact_messages(
        history,
        effective_context_tokens=400,
        system_prompt_tokens=20,
        output_reserve_tokens=50,
        recent_n=3,
        top_k=2,
    )
    assert result.compacted is True
    assert len(result.retrieval_results) == 2
    # First message is the synthesized substitution block (assistant
    # role — see _synthesize_retrieval_block for why).
    head = result.messages[0]
    assert head["role"] == "assistant"
    assert "Earlier chat A" in head["content"]
    assert "Old context A" in head["content"]
    assert "Earlier chat B" in head["content"]
    # The kept window's first message must remain a real user turn so
    # the assembled list alternates assistant(synth) → user(real).
    assert result.messages[1]["role"] == "user"


async def test_vault_failure_does_not_break_compaction(monkeypatch):
    async def _exploding_vault(*args, **kwargs):
        raise RuntimeError("vault boom")

    monkeypatch.setattr(
        "services.retrieval.sessions.find_earlier_turn_context",
        _exploding_vault,
    )

    history = _make_history(num_user_turns=10, body_chars=200)
    result = await compact_messages(
        history,
        effective_context_tokens=400,
        system_prompt_tokens=20,
        output_reserve_tokens=50,
        recent_n=3,
        top_k=3,
    )
    assert result.compacted is True
    assert result.retrieval_results == []
    # No synthesized prefix when retrieval failed; first message is the
    # first kept real user turn.
    assert result.messages[0]["role"] == "user"
    assert isinstance(result.messages[0]["content"], str)


# ── <think> stripping ──────────────────────────────────────────────────


async def test_think_blocks_stripped_even_without_compaction():
    history = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "<think>private scratch</think>Hi there"},
        {"role": "user", "content": "How are you?"},
    ]
    result = await compact_messages(
        history,
        effective_context_tokens=32_768,
        system_prompt_tokens=100,
    )
    assert result.compacted is False
    cleaned_assistant = result.messages[1]
    assert cleaned_assistant["content"] == "Hi there"


async def test_pure_think_assistant_turn_dropped():
    history = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "<think>only scratch nothing else</think>"},
        {"role": "user", "content": "Follow up"},
    ]
    result = await compact_messages(
        history,
        effective_context_tokens=32_768,
        system_prompt_tokens=100,
    )
    # Pure-think turn is removed so the next assistant message can
    # follow the user turn cleanly.
    assert all(m.get("content") != "<think>only scratch nothing else</think>" for m in result.messages)
    roles = [m["role"] for m in result.messages]
    assert roles == ["user", "user"]


# ── Audit event payload ───────────────────────────────────────────────


def test_compaction_result_as_event_carries_required_fields():
    result = CompactionResult(
        messages=[],
        compacted=True,
        turns_dropped=4,
        summary_used=False,
        recent_window_size=8,
        effective_ctx=32_768,
        tokens_before=20_000,
        tokens_after=4_500,
        threshold_pct=0.70,
        retrieval_results=[{"path": "conversations/a.md", "title": "x", "snippet": "y"}],
        reason="compacted",
    )
    event = result.as_event()
    assert "timestamp" in event
    assert event["turns_dropped"] == 4
    assert event["summary_used"] is False
    assert event["recent_window_size"] == 8
    assert event["effective_ctx_at_event"] == 32_768
    assert event["tokens_before"] == 20_000
    assert event["tokens_after"] == 4_500
    assert event["threshold_pct"] == 0.70
    assert event["retrieval_paths"] == ["conversations/a.md"]
    assert event["reason"] == "compacted"


# ── Defaults / env-var resolution ─────────────────────────────────────


async def test_threshold_env_var_override(monkeypatch):
    monkeypatch.setenv("JARVIS_COMPACTION_THRESHOLD_PCT", "0.50")
    history = _make_history(num_user_turns=2, body_chars=20)
    result = await compact_messages(
        history,
        effective_context_tokens=32_768,
        system_prompt_tokens=100,
    )
    assert result.threshold_pct == 0.50


async def test_threshold_env_var_out_of_range_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("JARVIS_COMPACTION_THRESHOLD_PCT", "1.50")
    history = _make_history(num_user_turns=2, body_chars=20)
    result = await compact_messages(
        history,
        effective_context_tokens=32_768,
        system_prompt_tokens=100,
    )
    assert result.threshold_pct == DEFAULT_THRESHOLD_PCT


async def test_threshold_env_var_non_float_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("JARVIS_COMPACTION_THRESHOLD_PCT", "not-a-number")
    history = _make_history(num_user_turns=2, body_chars=20)
    result = await compact_messages(
        history,
        effective_context_tokens=32_768,
        system_prompt_tokens=100,
    )
    assert result.threshold_pct == DEFAULT_THRESHOLD_PCT


async def test_recent_n_env_var_override(monkeypatch):
    monkeypatch.setenv("JARVIS_COMPACTION_RECENT_N", "2")
    # Do NOT pass recent_n explicitly — the env var must take effect.
    async def _no_vault_results(*args, **kwargs):
        return []

    monkeypatch.setattr(
        "services.retrieval.sessions.find_earlier_turn_context",
        _no_vault_results,
    )
    history = _make_history(num_user_turns=10, body_chars=200)
    result = await compact_messages(
        history,
        effective_context_tokens=400,
        system_prompt_tokens=20,
        output_reserve_tokens=50,
        top_k=0,
    )
    assert result.recent_window_size == 2


def test_module_defaults_match_adr_010_gate_verdict():
    """Pin the gate-validated config so a quiet refactor doesn't drift away.

    ADR 010 verdict (run-20260428T112547Z) picked recent_n=8 / top_k=3 /
    threshold=0.70 as the production canonical config. Changing any of
    these is an ADR-worthy decision.
    """
    assert DEFAULT_RECENT_N == 8
    assert DEFAULT_TOP_K == 3
    assert DEFAULT_THRESHOLD_PCT == 0.70


# ── Recent-N ceiling clamp ────────────────────────────────────────────


async def test_recent_n_above_ceiling_is_clamped(monkeypatch):
    """A misconfigured very-large recent_n must clamp, not silently
    disable compaction by always exceeding history length."""
    async def _no_vault_results(*args, **kwargs):
        return []

    monkeypatch.setattr(
        "services.retrieval.sessions.find_earlier_turn_context",
        _no_vault_results,
    )

    history = _make_history(num_user_turns=10, body_chars=200)
    result = await compact_messages(
        history,
        effective_context_tokens=400,
        system_prompt_tokens=20,
        output_reserve_tokens=50,
        recent_n=999_999,
        top_k=0,
    )
    # Clamped to the ceiling (200), still > 10 turns of history,
    # so no compaction fires — but the path is "recent_window_already_minimal",
    # not silent disable, and the operator gets a logged warning.
    assert result.recent_window_size <= 10  # equals user-turn count, since clamp > turns
    assert result.compacted is False


async def test_recent_n_env_var_above_ceiling_is_clamped(monkeypatch):
    monkeypatch.setenv("JARVIS_COMPACTION_RECENT_N", "1000000")
    async def _no_vault_results(*args, **kwargs):
        return []

    monkeypatch.setattr(
        "services.retrieval.sessions.find_earlier_turn_context",
        _no_vault_results,
    )

    history = _make_history(num_user_turns=300, body_chars=20)
    result = await compact_messages(
        history,
        effective_context_tokens=400,
        system_prompt_tokens=20,
        output_reserve_tokens=50,
        top_k=0,
    )
    # 300 user turns > 200 ceiling → clamp engages and compaction fires.
    assert result.recent_window_size == 200
    assert result.compacted is True


# ── Boundary: recent_n exactly equal to user-turn count ───────────────


async def test_recent_n_equals_user_turn_count_is_minimal_path():
    """When the conversation has *exactly* recent_n user turns, compaction
    treats it as "already minimal" — even if the budget is exceeded.
    Pin this so the strict-inequality vs ≤ branch isn't silently flipped."""
    history = _make_history(num_user_turns=8, body_chars=4000)  # very long body forces over-budget
    result = await compact_messages(
        history,
        effective_context_tokens=400,
        system_prompt_tokens=20,
        output_reserve_tokens=50,
        recent_n=8,
        top_k=0,
    )
    assert result.compacted is False
    assert result.reason == "recent_window_already_minimal"


# ── Back-to-back think-only assistant turns ───────────────────────────


async def test_consecutive_think_only_assistant_turns_dropped():
    """Two adjacent pure-think assistant turns are both dropped, and
    the surviving mixed turn keeps its real reply. This pins what
    ``_strip_think_blocks`` does on an unusual history shape; it does
    NOT promise role alternation across the result, since dropping
    both pure-think turns from a ``[user, assistant, assistant, user]``
    prefix collapses to ``[user, user, …]`` by construction. See
    ``test_strip_preserves_alternation_on_normal_history`` for the
    role-alternation contract on realistic conversation shapes.
    """
    history = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "<think>scratch one</think>"},
        {"role": "assistant", "content": "<think>scratch two</think>"},
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "<think>third scratch</think>real reply"},
    ]
    result = await compact_messages(
        history,
        effective_context_tokens=32_768,
        system_prompt_tokens=100,
    )
    # Both pure-think assistant turns must be gone; the mixed third
    # keeps its real reply.
    contents = [m.get("content") for m in result.messages]
    assert "<think>scratch one</think>" not in contents
    assert "<think>scratch two</think>" not in contents
    assert "real reply" in contents


async def test_strip_preserves_alternation_on_normal_history():
    """On a realistically-alternating history (the only shape we ever
    see in production), think-stripping must never produce two
    consecutive same-role messages of either kind. Compaction's
    downstream provider (Ollama chat templates) tolerates consecutive
    same-role messages but some adapters silently merge them; this
    test pins that the strip step doesn't introduce that hazard for
    real conversations.
    """
    history = [
        {"role": "user", "content": "Q1"},
        {"role": "assistant", "content": "<think>scratch</think>A1"},
        {"role": "user", "content": "Q2"},
        {"role": "assistant", "content": "<think>more scratch</think>A2"},
        {"role": "user", "content": "Q3"},
        {"role": "assistant", "content": "A3"},
    ]
    result = await compact_messages(
        history,
        effective_context_tokens=32_768,
        system_prompt_tokens=100,
    )
    roles = [m["role"] for m in result.messages]
    for i in range(len(roles) - 1):
        assert roles[i] != roles[i + 1], (
            f"adjacent same-role pair at index {i}: {roles}"
        )
    # And the real replies survived the strip.
    contents = [m.get("content") for m in result.messages]
    assert "A1" in contents and "A2" in contents and "A3" in contents
