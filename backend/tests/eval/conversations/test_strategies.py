"""Tests for eval-side ContextStrategy implementations (ADR 010)."""

from __future__ import annotations

import pytest

from services.chat import ContextStrategy

from tests.eval.conversations.strategies import (
    NaiveTruncateStrategy,
    RetrievalSubstitutionV1Strategy,
    _is_real_user_turn,
    _tokenize_for_retrieval,
)


# ── Protocol satisfaction ────────────────────────────────────────────────────


def test_naive_truncate_satisfies_context_strategy_protocol():
    assert isinstance(NaiveTruncateStrategy(recent_n=8), ContextStrategy)


def test_naive_truncate_name_includes_n():
    """The ``name`` is embedded in baseline filenames; different N values
    must produce distinguishable strategies."""
    assert NaiveTruncateStrategy(recent_n=4).name == "naive-truncate-4"
    assert NaiveTruncateStrategy(recent_n=12).name == "naive-truncate-12"


def test_naive_truncate_rejects_non_positive_n():
    with pytest.raises(ValueError, match="recent_n must be > 0"):
        NaiveTruncateStrategy(recent_n=0)
    with pytest.raises(ValueError, match="recent_n must be > 0"):
        NaiveTruncateStrategy(recent_n=-3)


# ── _is_real_user_turn helper ────────────────────────────────────────────────


def test_real_user_turn_for_string_content():
    assert _is_real_user_turn({"role": "user", "content": "Hi"})


def test_real_user_turn_for_text_block_list():
    msg = {"role": "user", "content": [{"type": "text", "text": "Hi"}]}
    assert _is_real_user_turn(msg)


def test_not_real_user_turn_for_tool_result_only_message():
    """A user message containing only a tool_result block is a protocol
    response, not a real turn — excluded from the recent-N count."""
    msg = {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "u1", "content": "out"}
        ],
    }
    assert not _is_real_user_turn(msg)


def test_not_real_user_turn_for_assistant():
    assert not _is_real_user_turn({"role": "assistant", "content": "Hi"})


def test_real_user_turn_for_mixed_content():
    """If a user message somehow has both text and tool_result blocks, it
    counts as a user turn — the user said something AND attached a tool
    response. (Rare but defined.)"""
    msg = {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "u1", "content": "out"},
            {"type": "text", "text": "and here is more context"},
        ],
    }
    assert _is_real_user_turn(msg)


# ── Truncation behavior ──────────────────────────────────────────────────────


def _alternating(n_turns: int) -> list[dict]:
    """Build a simple n-turn alternating user/assistant conversation."""
    msgs: list[dict] = []
    for i in range(n_turns):
        msgs.append({"role": "user", "content": f"user_{i}"})
        msgs.append({"role": "assistant", "content": f"asst_{i}"})
    return msgs


def test_truncate_returns_full_history_when_under_recent_n():
    strategy = NaiveTruncateStrategy(recent_n=8)
    messages = _alternating(3)
    out = strategy.assemble(messages)
    assert out == messages
    assert out is not messages  # defensive copy


def test_truncate_keeps_only_last_n_user_turns():
    strategy = NaiveTruncateStrategy(recent_n=3)
    messages = _alternating(10)  # 10 user turns
    out = strategy.assemble(messages)
    user_turns_kept = [m for m in out if _is_real_user_turn(m)]
    assert len(user_turns_kept) == 3
    # The kept user turns must be the LAST 3, not the first 3
    assert user_turns_kept[0]["content"] == "user_7"
    assert user_turns_kept[-1]["content"] == "user_9"


def test_truncate_preserves_messages_after_cut_point():
    """Everything from the (recent_n)-th-to-last user turn onward is kept
    intact — including the assistant message that follows each kept user
    turn."""
    strategy = NaiveTruncateStrategy(recent_n=2)
    messages = _alternating(5)  # 10 messages: u0,a0,u1,a1,u2,a2,u3,a3,u4,a4
    out = strategy.assemble(messages)
    # Should keep u3,a3,u4,a4 (4 messages — last 2 user turns + their assistants)
    assert len(out) == 4
    assert out[0] == {"role": "user", "content": "user_3"}
    assert out[-1] == {"role": "assistant", "content": "asst_4"}


def test_truncate_preserves_tool_pairs_within_kept_suffix():
    """Tool_use / tool_result pairs that fall within a kept user turn's
    span must remain intact. The Anthropic protocol rejects tool_use
    without matching tool_result; this test pins that the cut never
    falls between them."""
    messages = [
        {"role": "user", "content": "user_0"},
        {"role": "assistant", "content": "asst_0"},
        {"role": "user", "content": "user_1"},
        # User_1's response involves a tool round
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "u1", "name": "read_note", "input": {}}
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "u1", "content": "OK"}
            ],
        },
        {"role": "assistant", "content": "asst_1_final"},
        {"role": "user", "content": "user_2"},
        {"role": "assistant", "content": "asst_2"},
    ]
    strategy = NaiveTruncateStrategy(recent_n=2)
    out = strategy.assemble(messages)
    # We keep last 2 user turns (user_1, user_2). The tool round between
    # user_1 and user_2 must survive intact.
    tool_use_ids = [
        b["id"]
        for m in out
        if isinstance(m.get("content"), list)
        for b in m["content"]
        if isinstance(b, dict) and b.get("type") == "tool_use"
    ]
    tool_result_ids = [
        b["tool_use_id"]
        for m in out
        if isinstance(m.get("content"), list)
        for b in m["content"]
        if isinstance(b, dict) and b.get("type") == "tool_result"
    ]
    assert tool_use_ids == ["u1"]
    assert tool_result_ids == ["u1"]


def test_truncate_does_not_count_tool_result_messages_as_user_turns():
    """A long tool-loop history must not falsely inflate the user-turn
    count and cause too aggressive a cut."""
    messages = [
        {"role": "user", "content": "user_0"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "u1", "name": "read_note", "input": {}}
            ],
        },
        {
            "role": "user",  # tool_result wrapper — NOT a user turn
            "content": [
                {"type": "tool_result", "tool_use_id": "u1", "content": "X"}
            ],
        },
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "u2", "name": "read_note", "input": {}}
            ],
        },
        {
            "role": "user",  # tool_result wrapper — NOT a user turn
            "content": [
                {"type": "tool_result", "tool_use_id": "u2", "content": "Y"}
            ],
        },
        {"role": "assistant", "content": "synthesis"},
        {"role": "user", "content": "user_1"},
        {"role": "assistant", "content": "asst_1"},
    ]
    # There are 2 real user turns. recent_n=2 should keep everything.
    strategy = NaiveTruncateStrategy(recent_n=2)
    out = strategy.assemble(messages)
    assert out == messages


def test_truncate_does_not_mutate_input():
    strategy = NaiveTruncateStrategy(recent_n=2)
    messages = _alternating(5)
    snapshot = [dict(m) for m in messages]
    strategy.assemble(messages)
    assert messages == snapshot


def test_truncate_handles_empty_history():
    strategy = NaiveTruncateStrategy(recent_n=4)
    assert strategy.assemble([]) == []


# ── End-to-end with a real fixture ───────────────────────────────────────────


def test_truncate_with_real_fixture_drops_early_topic():
    """Fixture #1's recall question references a topic from turn 1 of a
    30-turn chat. With recent_n=2, the cut point excludes that turn —
    confirming the strategy actually changes what the model sees, which
    is the whole point of the eval."""
    from pathlib import Path
    from tests.eval.conversations.runner import load_fixture

    fx = load_fixture(
        Path(__file__).parent / "fixtures" / "01-long-conv-shallow.json"
    )
    # Reconstruct what the runner's history would look like just before
    # the assistant_target turn (i.e., all turns except the target).
    history: list[dict] = []
    for turn in fx["turns"]:
        if turn.get("role") == "assistant_target":
            break
        if turn.get("role") == "user":
            history.append({"role": "user", "content": turn["content"]})
        elif turn.get("role") == "assistant_scripted":
            history.append({"role": "assistant", "content": turn.get("content", "")})

    strategy = NaiveTruncateStrategy(recent_n=2)
    out = strategy.assemble(history)

    # The original turn-1 mention of "renewable energy in Poland" must be
    # gone after truncation to recent_n=2.
    text_blob = " ".join(
        m.get("content", "") if isinstance(m.get("content"), str) else ""
        for m in out
    )
    assert "renewable energy" not in text_blob.lower()
    assert "poland" not in text_blob.lower()


# ── RetrievalSubstitutionV1 ──────────────────────────────────────────────────


def test_retrieval_substitution_satisfies_context_strategy_protocol():
    assert isinstance(
        RetrievalSubstitutionV1Strategy(recent_n=8, top_k=3), ContextStrategy
    )


def test_retrieval_substitution_name_includes_n_and_k():
    """Stable identifier — N and K both reach the baseline filename so
    parameter sweeps produce distinguishable artifacts."""
    s = RetrievalSubstitutionV1Strategy(recent_n=8, top_k=3)
    assert s.name == "retrieval-substitution-v1-n8-k3"
    s2 = RetrievalSubstitutionV1Strategy(recent_n=12, top_k=5)
    assert s2.name == "retrieval-substitution-v1-n12-k5"


def test_retrieval_substitution_rejects_invalid_params():
    with pytest.raises(ValueError, match="recent_n must be > 0"):
        RetrievalSubstitutionV1Strategy(recent_n=0)
    with pytest.raises(ValueError, match="top_k must be > 0"):
        RetrievalSubstitutionV1Strategy(recent_n=4, top_k=0)
    with pytest.raises(ValueError, match="min_overlap must be >= 0"):
        RetrievalSubstitutionV1Strategy(recent_n=4, top_k=2, min_overlap=-1)


def test_retrieval_substitution_returns_full_history_when_under_recent_n():
    """No drops → no retrieval needed → identical to full-history. The
    strategy must not synthesize a retrieval block on a short history."""
    strategy = RetrievalSubstitutionV1Strategy(recent_n=8, top_k=3)
    messages = _alternating(3)
    out = strategy.assemble(messages)
    assert out == messages


def test_retrieval_substitution_reintroduces_relevant_dropped_turn():
    """The load-bearing test: a long conversation has a topic in user_0
    that the recent-N window drops, but the latest user turn references
    that topic. Retrieval should re-introduce user_0 (and its assistant
    response) as a leading retrieval block."""
    messages = [
        {"role": "user", "content": "I'm researching offshore wind in the Baltic sea region for Poland."},
        {"role": "assistant", "content": "Got it — offshore wind in the Baltic, Polish context."},
        {"role": "user", "content": "What's a good lunch?"},
        {"role": "assistant", "content": "Grain bowl with halloumi."},
        {"role": "user", "content": "Hiking boots for muddy trails?"},
        {"role": "assistant", "content": "Salomon X Ultra 4 GTX."},
        {"role": "user", "content": "Pomodoro timer app?"},
        {"role": "assistant", "content": "Be Focused or built-in Clock app."},
        {"role": "user", "content": "Best espresso machine under £400?"},
        {"role": "assistant", "content": "Sage Bambino Plus or Gaggia Classic Pro."},
        # Latest user turn pulls back to the offshore-wind topic
        {"role": "user", "content": "Coming back to my Baltic offshore wind project — any starter readings?"},
    ]
    # recent_n=2 keeps only the last 2 user turns. user_0's offshore-wind
    # turn is in the dropped half. Retrieval should pull it back.
    strategy = RetrievalSubstitutionV1Strategy(recent_n=2, top_k=2, min_overlap=1)
    out = strategy.assemble(messages)

    # First message must be the synthesized retrieval block
    assert out[0]["role"] == "user"
    block = out[0]["content"]
    assert "Retrieved earlier-conversation context" in block
    assert "offshore wind in the Baltic" in block.lower() or "baltic sea region" in block.lower()
    # And its assistant response should be carried alongside
    assert "polish context" in block.lower()


def test_retrieval_substitution_skips_block_when_no_overlap():
    """If no dropped turn shares any content tokens with the latest user
    turn, retrieval contributes nothing — the strategy must NOT add an
    empty/noise retrieval block. Behavior degenerates to naive-truncate."""
    messages = [
        {"role": "user", "content": "Espresso machines and grind sizes."},
        {"role": "assistant", "content": "Bambino Plus, medium grind."},
        {"role": "user", "content": "Pomodoro timer apps."},
        {"role": "assistant", "content": "Be Focused."},
        {"role": "user", "content": "Houseplants for low light."},
        {"role": "assistant", "content": "ZZ plant."},
        {"role": "user", "content": "Tell me about quantum entanglement experiments."},  # nothing shared
    ]
    strategy = RetrievalSubstitutionV1Strategy(recent_n=1, top_k=2, min_overlap=1)
    out = strategy.assemble(messages)
    # No retrieval block prepended — first message is still a real user turn.
    assert "Retrieved earlier-conversation context" not in out[0].get("content", "")


def test_retrieval_substitution_chronological_order_in_synthesized_block():
    """When two dropped turns are both retrieved, they must appear in the
    block in the order they originally occurred. Out-of-order context
    confuses the model; recency-of-mention is meaningful."""
    messages = [
        {"role": "user", "content": "Earlier point: my project codename is Albatross-9."},
        {"role": "assistant", "content": "Albatross-9, noted."},
        {"role": "user", "content": "Random unrelated chat."},
        {"role": "assistant", "content": "Sure."},
        {"role": "user", "content": "Later point: the Albatross-9 budget is £47,500."},
        {"role": "assistant", "content": "£47,500 budget for Albatross-9."},
        {"role": "user", "content": "More unrelated stuff."},
        {"role": "assistant", "content": "Right."},
        {"role": "user", "content": "Quick — what was the Albatross-9 budget I mentioned earlier?"},
    ]
    strategy = RetrievalSubstitutionV1Strategy(recent_n=1, top_k=2, min_overlap=1)
    out = strategy.assemble(messages)
    block = out[0]["content"]
    # Both retrieved turns must be present
    assert "codename" in block.lower()
    assert "47,500" in block or "£47,500" in block
    # Codename mention came first chronologically; budget came after.
    codename_pos = block.lower().find("codename")
    budget_pos = block.find("47,500")
    assert codename_pos < budget_pos


def test_retrieval_substitution_does_not_mutate_input():
    strategy = RetrievalSubstitutionV1Strategy(recent_n=2, top_k=2)
    messages = _alternating(5)
    snapshot = [dict(m) for m in messages]
    strategy.assemble(messages)
    assert messages == snapshot


def test_retrieval_substitution_skips_tool_result_messages_as_query():
    """The "query" used for retrieval is the latest *real* user turn, not
    a tool_result wrapper. This pins behavior in tool-loop fixtures where
    a tool_result is the last user-role message in history before the
    target turn."""
    messages = [
        {"role": "user", "content": "Northwind Trading deadline 17 June 2026."},
        {"role": "assistant", "content": "Logged."},
        {"role": "user", "content": "Random chat one."},
        {"role": "assistant", "content": "Sure."},
        {"role": "user", "content": "Random chat two."},
        {"role": "assistant", "content": "OK."},
        {"role": "user", "content": "Refresh the Northwind matter context, please."},  # real query
        # Then a tool round — last user-role msg is a tool_result wrapper
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "u1", "name": "read_note", "input": {}}],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "u1", "content": "(empty)"}],
        },
    ]
    strategy = RetrievalSubstitutionV1Strategy(recent_n=1, top_k=2, min_overlap=1)
    out = strategy.assemble(messages)
    assert out[0]["role"] == "user"
    block = out[0]["content"]
    # The "Northwind" detail should be re-introduced; query was the real
    # user turn, not the tool_result wrapper.
    assert "Northwind" in block


# ── Tokenizer ────────────────────────────────────────────────────────────────


def test_tokenizer_filters_stop_words_and_short_tokens():
    """The tokenizer's filters are load-bearing: a query of "the and you"
    would otherwise overlap with every conversation in the corpus."""
    tokens = _tokenize_for_retrieval(
        "The Project Albatross-9 is the codename for our work.", min_len=3
    )
    # Stop-words removed; project + albatross + codename + work survive.
    assert "the" not in tokens
    assert "for" not in tokens
    assert "our" not in tokens
    assert "project" in tokens
    assert "albatross" in tokens
    assert "codename" in tokens


def test_tokenizer_preserves_polish_diacritics():
    """Fixture #5 and #15 are bilingual EN/PL. The tokenizer must not
    drop tokens like ``sygnatura`` or ``Kraków`` (which would silently
    weaken retrieval on those fixtures)."""
    tokens = _tokenize_for_retrieval(
        "Sygnatura sprawy I C 1247/23, sąd okręgowy Kraków.", min_len=3
    )
    assert "sygnatura" in tokens
    assert "kraków" in tokens or "krakow" in tokens
    assert "sprawy" in tokens