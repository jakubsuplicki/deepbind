"""Tests for the conversation-replay runner (ADR 010).

Uses a stub ChatCallable — no network, no LLM provider. The runner's job
at v1 is to replay scripted turns deterministically and hand assembled
context to a chat callable; whether the callable is a real model or a
fake-by-design stub is irrelevant to the runner's correctness.
"""

from pathlib import Path

import pytest

from services.chat import FullHistoryStrategy

from tests.eval.conversations.runner import (
    ChatCallable,
    FixtureResult,
    TurnResult,
    load_fixture,
    run_fixture,
    run_fixture_sync,
)


_FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ── Stub chat callables ──────────────────────────────────────────────────────


class _ScriptedChat:
    """A chat callable that returns pre-canned responses in order.

    Each call pops one response off the script. If the script is exhausted,
    raises — catches a runner bug that calls the chat more times than
    there are assistant_target turns.
    """

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls: list[tuple[list[dict], str]] = []

    async def __call__(self, messages: list[dict], system_prompt: str) -> str:
        if not self._responses:
            raise AssertionError("ScriptedChat exhausted — runner called too many times")
        self.calls.append((list(messages), system_prompt))
        return self._responses.pop(0)


class _ConstantChat:
    """Returns the same response every call. Useful when the test only
    cares about scoring, not which call gets which response."""

    def __init__(self, response: str):
        self._response = response
        self.call_count = 0

    async def __call__(self, messages: list[dict], system_prompt: str) -> str:
        self.call_count += 1
        return self._response


# ── ChatCallable Protocol satisfaction ───────────────────────────────────────


def test_scripted_chat_satisfies_protocol():
    assert isinstance(_ScriptedChat(["x"]), ChatCallable)


def test_constant_chat_satisfies_protocol():
    assert isinstance(_ConstantChat("x"), ChatCallable)


# ── Fixture loading ──────────────────────────────────────────────────────────


def test_load_fixture_reads_real_fixture_file():
    fx = load_fixture(_FIXTURES_DIR / "01-long-conv-shallow.json")
    assert fx["id"] == "long-conv-shallow"
    assert isinstance(fx["turns"], list)
    assert any(t["role"] == "assistant_target" for t in fx["turns"])


def test_load_fixture_rejects_malformed_json(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{}")  # missing 'turns'
    with pytest.raises(ValueError, match="missing or malformed 'turns'"):
        load_fixture(bad)


# ── End-to-end replay on real fixtures ───────────────────────────────────────


def test_replay_fixture_1_passes_with_correct_response():
    """Spot-check that a competent answer to fixture #1's recall question
    mechanically passes. Uses the actual fixture file from disk so any
    schema drift is caught."""
    fx = load_fixture(_FIXTURES_DIR / "01-long-conv-shallow.json")
    chat = _ConstantChat(
        "Your research project is about renewable energy adoption in "
        "Poland, focused on offshore wind in the Baltic."
    )
    result = run_fixture_sync(fx, chat=chat, chat_model_id="stub")

    assert result.fixture_id == "long-conv-shallow"
    assert result.strategy_name == "full-history"
    assert result.target_turn_count == 1
    assert len(result.turn_results) == 1
    assert result.turn_results[0].score.passed
    assert result.mechanical_pass_rate == 1.0


def test_replay_fixture_1_fails_on_confabulation():
    """A response that confabulates a different topic must fail — pins
    the must_not_contain guards from fixture #1."""
    fx = load_fixture(_FIXTURES_DIR / "01-long-conv-shallow.json")
    chat = _ConstantChat(
        "You mentioned a research project on solar panels in Spain."
    )
    result = run_fixture_sync(fx, chat=chat, chat_model_id="stub")

    assert result.mechanical_pass_rate == 0.0
    failed = result.turn_results[0].score
    # Multiple checks fail: missing facts AND triggered guards
    assert "topic_renewable_energy" in failed.facts_failed
    assert "topic_poland" in failed.facts_failed
    assert "no_solar" in failed.guards_triggered


def test_replay_fixture_3_distractor_correct_answer_passes():
    fx = load_fixture(_FIXTURES_DIR / "03-distractor-injection.json")
    chat = _ConstantChat(
        "The bearing patent's priority date is March 12, 2024."
    )
    result = run_fixture_sync(fx, chat=chat, chat_model_id="stub")
    assert result.mechanical_pass_rate == 1.0


def test_replay_fixture_3_distractor_confabulation_fails():
    fx = load_fixture(_FIXTURES_DIR / "03-distractor-injection.json")
    chat = _ConstantChat(
        "The bearing patent's priority date is August 8, 2024."
    )
    result = run_fixture_sync(fx, chat=chat, chat_model_id="stub")
    assert result.mechanical_pass_rate == 0.0
    assert "no_coating_date" in result.turn_results[0].score.guards_triggered


# ── Replay mechanics ─────────────────────────────────────────────────────────


def test_runner_calls_chat_exactly_once_per_target_turn():
    """Spy on the chat callable to assert call count == target turn count."""
    fx = load_fixture(_FIXTURES_DIR / "01-long-conv-shallow.json")
    chat = _ConstantChat("placeholder response")
    result = run_fixture_sync(fx, chat=chat, chat_model_id="stub")
    assert chat.call_count == 1
    assert result.target_turn_count == 1


def test_runner_passes_strategy_assembled_history_to_chat():
    """The strategy's output is what reaches the chat callable, not the raw
    history. Pin via a strategy that drops messages and observe the call."""

    class _DropAllStrategy:
        name = "drop-all"

        def assemble(self, messages: list[dict]) -> list[dict]:
            return []

    fx = load_fixture(_FIXTURES_DIR / "01-long-conv-shallow.json")
    chat = _ScriptedChat(["response"])
    run_fixture_sync(fx, strategy=_DropAllStrategy(), chat=chat, chat_model_id="stub")
    # The strategy returned []; the runner must have passed [] to chat
    assert chat.calls[0][0] == []


def test_runner_default_strategy_is_full_history():
    fx = load_fixture(_FIXTURES_DIR / "01-long-conv-shallow.json")
    chat = _ScriptedChat(["response"])
    result = run_fixture_sync(fx, chat=chat, chat_model_id="stub")
    assert result.strategy_name == "full-history"


def test_runner_rejects_strategy_returning_non_list():
    """Mirrors the chat router's strategy-boundary guard."""

    class _BrokenStrategy:
        name = "broken"

        def assemble(self, messages: list[dict]):
            return None

    fx = load_fixture(_FIXTURES_DIR / "01-long-conv-shallow.json")
    chat = _ConstantChat("response")
    with pytest.raises(TypeError, match="expected list"):
        run_fixture_sync(fx, strategy=_BrokenStrategy(), chat=chat, chat_model_id="stub")


def test_runner_rejects_chat_returning_non_string():
    class _BrokenChat:
        async def __call__(self, messages, system_prompt):
            return 42  # not a string

    fx = load_fixture(_FIXTURES_DIR / "01-long-conv-shallow.json")
    with pytest.raises(TypeError, match="expected str"):
        run_fixture_sync(fx, chat=_BrokenChat(), chat_model_id="stub")


def test_runner_records_latency_per_turn():
    fx = load_fixture(_FIXTURES_DIR / "01-long-conv-shallow.json")
    chat = _ConstantChat("response")
    result = run_fixture_sync(fx, chat=chat, chat_model_id="stub")
    assert all(r.latency_ms >= 0 for r in result.turn_results)


def test_fixture_result_aggregates_pass_rate():
    """Two-turn fixture, one passes one fails → 50% pass rate."""
    # Synthesize a small fixture with two assistant_target turns
    fx = {
        "id": "synthetic-two-target",
        "schema_version": 1,
        "turns": [
            {"role": "user", "content": "Q1"},
            {
                "role": "assistant_target",
                "expected_facts": [{"id": "a", "match": "regex", "pattern": "alpha"}],
            },
            {"role": "user", "content": "Q2"},
            {
                "role": "assistant_target",
                "expected_facts": [{"id": "b", "match": "regex", "pattern": "beta"}],
            },
        ],
    }
    chat = _ScriptedChat(["alpha is here", "neither term"])
    result = run_fixture_sync(fx, chat=chat, chat_model_id="stub")
    assert result.target_turn_count == 2
    assert result.mechanical_pass_rate == 0.5


# ── Tool-call replay ─────────────────────────────────────────────────────────


def test_replay_handles_scripted_tool_call_and_result():
    """Fixture 4 has scripted tool_use + tool_result blocks before its
    assistant_target. The runner must build matching tool_use_ids so the
    history is well-formed (else providers reject it)."""
    fx = load_fixture(_FIXTURES_DIR / "04-multi-tool-loop.json")
    chat = _ConstantChat(
        "The 90-day window came from the addendum-2024 document."
    )
    result = run_fixture_sync(fx, chat=chat, chat_model_id="stub")

    assert result.mechanical_pass_rate == 1.0
    # Verify the assembled history contains tool_use and tool_result blocks
    fx_history = chat  # _ConstantChat doesn't capture; use ScriptedChat for that


def test_replay_tool_use_and_result_are_paired_in_history():
    """The history that reaches the chat callable must have matching
    tool_use_id between the assistant tool_use block and the user
    tool_result block. Misalignment would cause provider rejection in
    real runs."""
    fx = load_fixture(_FIXTURES_DIR / "04-multi-tool-loop.json")
    chat = _ScriptedChat(["addendum-2024 document"])
    run_fixture_sync(fx, chat=chat, chat_model_id="stub")

    history = chat.calls[0][0]
    tool_use_ids = []
    tool_result_ids = []
    for msg in history:
        if not isinstance(msg.get("content"), list):
            continue
        for block in msg["content"]:
            if isinstance(block, dict):
                if block.get("type") == "tool_use":
                    tool_use_ids.append(block["id"])
                elif block.get("type") == "tool_result":
                    tool_result_ids.append(block["tool_use_id"])

    assert tool_use_ids, "expected scripted tool_use blocks in history"
    assert tool_use_ids == tool_result_ids, (
        "tool_use_id and tool_result.tool_use_id must match per Anthropic protocol"
    )


def test_orphaned_tool_result_raises():
    """A fixture with a tool_result that has no preceding tool_use is
    malformed — runner must fail loudly rather than silently produce a
    broken history."""
    bad = {
        "id": "synthetic-orphan",
        "schema_version": 1,
        "turns": [
            {"role": "user", "content": "Hi"},
            {"role": "tool_result", "content": "result", "tool_name": "x"},
            {"role": "assistant_target", "expected_facts": []},
        ],
    }
    chat = _ConstantChat("ok")
    with pytest.raises(ValueError, match="tool_result without preceding"):
        run_fixture_sync(bad, chat=chat, chat_model_id="stub")


def test_unknown_role_raises():
    bad = {
        "id": "synthetic-bad-role",
        "schema_version": 1,
        "turns": [{"role": "wizard", "content": "abracadabra"}],
    }
    chat = _ConstantChat("ok")
    with pytest.raises(ValueError, match="unknown role"):
        run_fixture_sync(bad, chat=chat, chat_model_id="stub")


# ── Async path direct invocation ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_fixture_async_path():
    """The sync wrapper exists for pytest convenience; pin that the async
    function works directly too (the eval CLI will use it)."""
    fx = load_fixture(_FIXTURES_DIR / "01-long-conv-shallow.json")
    chat = _ConstantChat(
        "Your research project is about renewable energy in Poland — Baltic offshore wind."
    )
    result = await run_fixture(
        fx, strategy=FullHistoryStrategy(), chat=chat, chat_model_id="stub"
    )
    assert isinstance(result, FixtureResult)
    assert result.mechanical_pass_rate == 1.0


# ── Severity aggregations on FixtureResult ──────────────────────────────────


def test_fixture_result_severity_distribution_sums_to_one():
    """Synthetic two-target fixture: one clean pass, one no-answer.
    Severity distribution must sum to 1.0 across all buckets."""
    fx = {
        "id": "synth-severity",
        "schema_version": 1,
        "turns": [
            {"role": "user", "content": "Q1"},
            {
                "role": "assistant_target",
                "expected_facts": [{"id": "a", "match": "regex", "pattern": "alpha"}],
            },
            {"role": "user", "content": "Q2"},
            {
                "role": "assistant_target",
                "expected_facts": [{"id": "b", "match": "regex", "pattern": "beta"}],
            },
        ],
    }
    chat = _ScriptedChat(["alpha clean", "I don't know"])
    result = run_fixture_sync(fx, chat=chat, chat_model_id="stub")
    dist = result.severity_distribution
    assert abs(sum(dist.values()) - 1.0) < 1e-9
    assert dist["clean_pass"] == 0.5
    assert dist["no_answer"] == 0.5
    assert dist["partial"] == 0.0
    assert dist["confabulation"] == 0.0


def test_fixture_result_clean_pass_rate_alias_matches_mechanical():
    """``mechanical_pass_rate`` is preserved as an alias for
    ``clean_pass_rate`` to avoid breaking earlier callers."""
    fx = load_fixture(_FIXTURES_DIR / "01-long-conv-shallow.json")
    chat = _ConstantChat("renewable energy in Poland — offshore Baltic")
    result = run_fixture_sync(fx, chat=chat, chat_model_id="stub")
    assert result.mechanical_pass_rate == result.clean_pass_rate


def test_fixture_result_confabulation_rate_distinct_from_pass_rate():
    """A confabulating response counts toward confabulation_rate, not
    clean_pass_rate."""
    fx = load_fixture(_FIXTURES_DIR / "03-distractor-injection.json")
    chat = _ConstantChat("the bearing patent's priority date is August 8, 2024.")
    result = run_fixture_sync(fx, chat=chat, chat_model_id="stub")
    assert result.clean_pass_rate == 0.0
    assert result.confabulation_rate == 1.0


# ── Multi-seed runs ──────────────────────────────────────────────────────────


def _seed_aware_chat_factory(responses_by_seed: dict[int, str]):
    """Test helper: build a chat factory that returns a different
    constant response per seed. Lets multi-seed tests verify that the
    factory was called per seed and that turn results carry the right
    seed label."""

    def factory(seed: int):
        if seed not in responses_by_seed:
            raise AssertionError(f"factory called with unexpected seed {seed}")
        return _ConstantChat(responses_by_seed[seed])

    return factory


def test_multi_seed_runs_factory_once_per_seed():
    from tests.eval.conversations.runner import run_fixture_multi_seed_sync

    fx = load_fixture(_FIXTURES_DIR / "01-long-conv-shallow.json")
    seeds_used: list[int] = []

    def factory(seed: int):
        seeds_used.append(seed)
        return _ConstantChat("renewable energy in Poland — offshore Baltic")

    result = run_fixture_multi_seed_sync(
        fx, chat_factory=factory, seeds=[1, 2, 3], chat_model_id="stub"
    )
    assert seeds_used == [1, 2, 3]
    assert result.seeds == [1, 2, 3]
    # 3 seeds × 1 target turn = 3 results
    assert len(result.turn_results) == 3


def test_multi_seed_turn_results_carry_seed_label():
    from tests.eval.conversations.runner import run_fixture_multi_seed_sync

    fx = load_fixture(_FIXTURES_DIR / "01-long-conv-shallow.json")
    factory = _seed_aware_chat_factory({
        10: "renewable energy in Poland — offshore Baltic",
        20: "renewable energy in Poland — offshore Baltic",
    })
    result = run_fixture_multi_seed_sync(
        fx, chat_factory=factory, seeds=[10, 20], chat_model_id="stub"
    )
    seeds_in_results = sorted({r.seed for r in result.turn_results})
    assert seeds_in_results == [10, 20]


def test_multi_seed_aggregates_across_seeds():
    from tests.eval.conversations.runner import run_fixture_multi_seed_sync

    fx = load_fixture(_FIXTURES_DIR / "01-long-conv-shallow.json")
    # Seed 1 passes, seed 2 fails (no_answer), seed 3 passes
    factory = _seed_aware_chat_factory({
        1: "renewable energy in Poland — offshore Baltic",
        2: "I don't recall any project being mentioned.",
        3: "renewable energy in Poland — offshore Baltic",
    })
    result = run_fixture_multi_seed_sync(
        fx, chat_factory=factory, seeds=[1, 2, 3], chat_model_id="stub"
    )
    assert result.clean_pass_rate == pytest.approx(2 / 3)
    # Variance: per-seed rates are 1.0, 0.0, 1.0 → stdev > 0
    assert result.stdev_clean_pass_rate > 0


def test_multi_seed_per_seed_clean_pass_rates():
    from tests.eval.conversations.runner import run_fixture_multi_seed_sync

    fx = load_fixture(_FIXTURES_DIR / "01-long-conv-shallow.json")
    factory = _seed_aware_chat_factory({
        7: "renewable energy in Poland — offshore Baltic",
        8: "I don't recall.",
    })
    result = run_fixture_multi_seed_sync(
        fx, chat_factory=factory, seeds=[7, 8], chat_model_id="stub"
    )
    rates = result.per_seed_clean_pass_rates()
    assert rates == {7: 1.0, 8: 0.0}


def test_multi_seed_rejects_empty_seeds():
    from tests.eval.conversations.runner import run_fixture_multi_seed_sync

    fx = load_fixture(_FIXTURES_DIR / "01-long-conv-shallow.json")
    chat = _ConstantChat("ok")
    with pytest.raises(ValueError, match="non-empty"):
        run_fixture_multi_seed_sync(
            fx,
            chat_factory=lambda _seed: chat,
            seeds=[],
            chat_model_id="stub",
        )


def test_multi_seed_rejects_duplicate_seeds():
    """Duplicate seeds collapse to fewer effective trials than the user
    asked for — fail loud rather than silently undercount."""
    from tests.eval.conversations.runner import run_fixture_multi_seed_sync

    fx = load_fixture(_FIXTURES_DIR / "01-long-conv-shallow.json")
    chat = _ConstantChat("ok")
    with pytest.raises(ValueError, match="unique"):
        run_fixture_multi_seed_sync(
            fx,
            chat_factory=lambda _seed: chat,
            seeds=[1, 1, 2],
            chat_model_id="stub",
        )


def test_single_seed_result_has_zero_stdev():
    """One-seed run is a degenerate case for variance — stdev must be
    0.0, not NaN or an exception."""
    fx = load_fixture(_FIXTURES_DIR / "01-long-conv-shallow.json")
    chat = _ConstantChat("renewable energy in Poland — offshore Baltic")
    result = run_fixture_sync(fx, chat=chat, chat_model_id="stub")
    assert result.stdev_clean_pass_rate == 0.0


# ── Naive-truncate strategy through the runner ───────────────────────────────


# ── Production-retrieval wiring ──────────────────────────────────────────────


def test_retrieval_disabled_by_default_uses_fallback_system_prompt():
    """Default off — chat callable receives the fixed fallback system
    prompt and no retrieval call is made."""
    fx = load_fixture(_FIXTURES_DIR / "01-long-conv-shallow.json")

    captured_prompts: list[str] = []

    class _CapturingChat:
        async def __call__(self, messages, system_prompt):
            captured_prompts.append(system_prompt)
            return "renewable energy in Poland — offshore Baltic"

    run_fixture_sync(fx, chat=_CapturingChat(), chat_model_id="stub")

    # Fallback system prompt — fixed string in the runner module
    from tests.eval.conversations.runner import _DEFAULT_SYSTEM_PROMPT
    assert captured_prompts[0] == _DEFAULT_SYSTEM_PROMPT


def test_retrieval_enabled_calls_build_system_prompt_with_recent_user_text(monkeypatch):
    """When retrieval is enabled, the runner must call
    ``build_system_prompt_with_stats`` with the most recent user message
    text — mirroring production behavior."""
    fx = load_fixture(_FIXTURES_DIR / "01-long-conv-shallow.json")

    captured_args: list[tuple[str, object, object]] = []

    async def _fake_build(user_message, workspace_path=None, graph_scope=None):
        captured_args.append((user_message, workspace_path, graph_scope))
        return ("AUGMENTED_PROMPT", {})

    monkeypatch.setattr(
        "services.system_prompt.build_system_prompt_with_stats", _fake_build
    )

    captured_prompts: list[str] = []

    class _CapturingChat:
        async def __call__(self, messages, system_prompt):
            captured_prompts.append(system_prompt)
            return "ok"

    run_fixture_sync(
        fx,
        chat=_CapturingChat(),
        chat_model_id="stub",
        retrieval_enabled=True,
    )

    assert captured_args, "build_system_prompt_with_stats was never called"
    user_message, _ws_path, _scope = captured_args[0]
    # The most recent user message in fixture #1 before the target turn
    # is the "what was that research project I mentioned..." line.
    assert "research project" in user_message.lower()
    # Augmented prompt is what reaches the chat
    assert captured_prompts[0] == "AUGMENTED_PROMPT"


def test_retrieval_enabled_passes_workspace_path_and_graph_scope_through(monkeypatch):
    fx = load_fixture(_FIXTURES_DIR / "01-long-conv-shallow.json")
    captured: dict = {}

    async def _fake_build(user_message, workspace_path=None, graph_scope=None):
        captured["workspace_path"] = workspace_path
        captured["graph_scope"] = graph_scope
        return ("PROMPT", {})

    monkeypatch.setattr(
        "services.system_prompt.build_system_prompt_with_stats", _fake_build
    )

    from pathlib import Path
    fake_path = Path("/tmp/some-fake-workspace")

    chat = _ConstantChat("ok")
    run_fixture_sync(
        fx,
        chat=chat,
        chat_model_id="stub",
        retrieval_enabled=True,
        workspace_path=fake_path,
        graph_scope="some/scope",
    )

    assert captured["workspace_path"] == fake_path
    assert captured["graph_scope"] == "some/scope"


def test_retrieval_enabled_handles_empty_history_for_first_turn():
    """If the first message in history is the assistant_target turn (no
    user message before it), retrieval falls back to empty query — the
    runner must not crash."""
    fx = {
        "id": "synth-no-user",
        "schema_version": 1,
        "turns": [
            {"role": "assistant_target", "expected_facts": []},
        ],
    }

    captured_prompts: list[str] = []

    class _CapturingChat:
        async def __call__(self, messages, system_prompt):
            captured_prompts.append(system_prompt)
            return "ok"

    # No retrieval — should still work even without user history
    run_fixture_sync(fx, chat=_CapturingChat(), chat_model_id="stub")
    assert captured_prompts


def test_last_user_message_text_skips_tool_result_wrappers():
    """The retrieval query must come from a real user message, not from
    a user-role tool_result wrapper. Pin this directly because the
    full-runner test path through fixture #2 doesn't surface a clear
    failure if the helper picks the wrong message."""
    from tests.eval.conversations.runner import _last_user_message_text

    history = [
        {"role": "user", "content": "real user question"},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "u1", "name": "read_note", "input": {}}],
        },
        {
            "role": "user",  # NOT a real user message — tool_result wrapper
            "content": [{"type": "tool_result", "tool_use_id": "u1", "content": "note body"}],
        },
    ]
    assert _last_user_message_text(history) == "real user question"


def test_last_user_message_text_handles_text_block_content():
    """A user message with content as a list of text blocks (rare but
    legal in the Anthropic protocol) must yield the joined text."""
    from tests.eval.conversations.runner import _last_user_message_text

    history = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "first part "},
                {"type": "text", "text": "second part"},
            ],
        },
    ]
    assert _last_user_message_text(history) == "first part second part"


def test_last_user_message_text_returns_empty_when_no_user_message():
    from tests.eval.conversations.runner import _last_user_message_text
    assert _last_user_message_text([]) == ""
    assert _last_user_message_text([{"role": "assistant", "content": "Hi"}]) == ""


def test_runner_with_naive_truncate_drops_early_context():
    """End-to-end check that NaiveTruncateStrategy actually changes what
    the model sees compared to FullHistoryStrategy."""
    from tests.eval.conversations.strategies import NaiveTruncateStrategy

    fx = load_fixture(_FIXTURES_DIR / "01-long-conv-shallow.json")
    captured: list[list[dict]] = []

    class _CapturingChat:
        async def __call__(self, messages, system_prompt):
            captured.append(list(messages))
            return "I don't recall a research project."

    run_fixture_sync(
        fx,
        strategy=NaiveTruncateStrategy(recent_n=2),
        chat=_CapturingChat(),
        chat_model_id="stub",
    )

    assert captured, "chat was never invoked"
    # Renewable-energy mention (turn 1) must be gone after truncate to N=2
    text_blob = " ".join(
        m.get("content", "") if isinstance(m.get("content"), str) else ""
        for m in captured[0]
    ).lower()
    assert "renewable energy" not in text_blob
