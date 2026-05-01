"""Pins ADR 009 amendment 2026-05-01 — stable system-prompt prefix.

The amendment moved the retrieved-context block out of the system prompt
and into the user-message position so Ollama's KV cache prefix-match can
reuse the long stable prefix on warm follow-up turns. These tests pin
that contract:

1. ``build_system_prompt_with_stats`` returns a system_prompt that does
   NOT contain retrieved-note content, even when retrieval has results.
2. The retrieval block is surfaced separately via ``stats["retrieval_block"]``.
3. The system_prompt is byte-identical across two calls with different
   user messages but the same persona / specialist / language posture
   — this is the load-bearing invariant that lets KV cache reuse work.
4. ``attach_retrieval_to_user_message`` glues the retrieval block onto
   the latest user message in the dispatched messages list, leaving the
   system prompt untouched.

If any of these assertions break, the prefix-match guarantee no longer
holds and the warm-turn TTFT regression returns. Don't loosen them
without an ADR amendment.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from services.system_prompt import (
    SYSTEM_PROMPT,
    attach_retrieval_to_user_message,
    build_system_prompt_with_stats,
)


# ── 1. system_prompt does not contain retrieval ──────────────────────────────


@pytest.mark.anyio
async def test_system_prompt_excludes_retrieved_context():
    """When retrieval returns content, it lives in stats['retrieval_block'],
    NOT in system_prompt. This is the fix for the prefix-instability bug."""
    fake_retrieval = (
        "<retrieved_note path=\"people/adam.md\">Adam runs the platform team.</retrieved_note>"
    )

    async def _fake_build_context(user_message, workspace_path=None):
        return fake_retrieval, len(fake_retrieval) // 4, [{"path": "people/adam.md"}]

    with patch("services.system_prompt.build_context", _fake_build_context):
        prompt, stats = await build_system_prompt_with_stats("who is Adam?")

    assert "Adam runs the platform team" not in prompt, (
        "Retrieval content leaked into the system prompt — prefix-match guarantee "
        "is broken. ADR 009 amendment 2026-05-01 mandates retrieval lives in the "
        "user-message position via attach_retrieval_to_user_message."
    )
    assert "<retrieved_note" not in prompt, (
        "<retrieved_note> wrapper leaked into system prompt"
    )
    assert "Adam runs the platform team" in stats["retrieval_block"], (
        "Retrieval content disappeared entirely — should be in stats['retrieval_block']"
    )
    assert "<retrieved_note" in stats["retrieval_block"], (
        "Retrieval block lost its <retrieved_note> XML wrapper"
    )


# ── 2. system_prompt is byte-identical across calls with different user msgs


@pytest.mark.anyio
async def test_system_prompt_is_byte_stable_across_turns():
    """Two calls with different user messages but identical persona /
    specialist posture and the same language must produce byte-identical
    system prompts. This is what lets Ollama reuse its KV cache prefix
    on warm follow-up turns. Prior to ADR 009 amendment 2026-05-01 the
    system prompt embedded retrieval, so it mutated turn-to-turn and
    the cache prefix-match failed at byte 0."""

    # Different retrieval per call (mirroring real-world: different user msg
    # → different BM25/cosine matches). The system_prompt must not be
    # affected by this mutation.
    call_count = {"n": 0}

    async def _varying_retrieval(user_message, workspace_path=None):
        call_count["n"] += 1
        text = f"<retrieved_note path=\"call{call_count['n']}.md\">payload {call_count['n']}</retrieved_note>"
        return text, len(text) // 4, [{"path": f"call{call_count['n']}.md"}]

    with patch("services.system_prompt.build_context", _varying_retrieval):
        prompt_1, stats_1 = await build_system_prompt_with_stats(
            "what did I save about kubernetes"
        )
        prompt_2, stats_2 = await build_system_prompt_with_stats(
            "what about helm charts"
        )

    assert prompt_1 == prompt_2, (
        "System prompt mutated between two calls in the same English-language "
        "session. The prefix-match guarantee is broken; warm-turn TTFT will "
        "regress to the pre-fix 7-8s floor on Apple M5 + Ollama 0.18.0."
    )
    # Retrieval did mutate (expected — different user message produced different
    # retrieval). That's fine; it goes in user-message position, not in the
    # prefix the cache reuses.
    assert stats_1["retrieval_block"] != stats_2["retrieval_block"]


@pytest.mark.anyio
async def test_system_prompt_unchanged_when_retrieval_empty():
    """No-retrieval path also produces a stable system_prompt. The "no
    matches" case used to skip the retrieved-notes prefix string but still
    produced a system prompt that varied with retrieval state — now there's
    no retrieved-notes block in the prompt at all, so this case collapses
    to the same shape."""

    async def _empty_retrieval(user_message, workspace_path=None):
        return "", 0, []

    with patch("services.system_prompt.build_context", _empty_retrieval):
        prompt_1, stats_1 = await build_system_prompt_with_stats("hi")
        prompt_2, stats_2 = await build_system_prompt_with_stats("how are you")

    assert prompt_1 == prompt_2
    assert stats_1["retrieval_block"] == ""
    assert stats_2["retrieval_block"] == ""
    # The base persona is the dominant chunk of the prompt — sanity-check it's there.
    assert SYSTEM_PROMPT.split("\n", 1)[0] in prompt_1


# ── 3. attach_retrieval_to_user_message helper ───────────────────────────────


def test_attach_retrieval_string_content():
    """Plain user message gets retrieval prepended with a blank line separator.
    The user's actual message stays intact at the end."""
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "what did I save about k8s"},
    ]
    block = "Here are potentially relevant notes from the user's memory:\n<retrieved_note path=\"k8s.md\">cluster bootstrap</retrieved_note>"

    out = attach_retrieval_to_user_message(messages, block)

    # First two messages untouched.
    assert out[0] == messages[0]
    assert out[1] == messages[1]
    # Last (latest) user message has retrieval prepended.
    assert out[2]["role"] == "user"
    assert out[2]["content"].startswith(block)
    assert out[2]["content"].endswith("what did I save about k8s")
    # Original list isn't mutated in place (caller may re-use it).
    assert messages[2]["content"] == "what did I save about k8s"


def test_attach_retrieval_empty_block_is_noop():
    """No retrieval → return the input unchanged (same list object, fast path)."""
    messages = [{"role": "user", "content": "hi"}]
    out = attach_retrieval_to_user_message(messages, "")
    assert out is messages


def test_attach_retrieval_skips_when_no_user_at_tail():
    """Defensive: if the last message isn't user-role, leave the list alone
    rather than corrupting an assistant or tool turn."""
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    out = attach_retrieval_to_user_message(messages, "block")
    assert out == messages


def test_attach_retrieval_structured_user_content():
    """User message with list-content (e.g. a tool_result block carrier)
    receives retrieval as a leading text block; existing blocks are
    preserved in their original positions."""
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
            ],
        },
    ]
    out = attach_retrieval_to_user_message(messages, "evidence")

    assert isinstance(out[0]["content"], list)
    assert out[0]["content"][0] == {"type": "text", "text": "evidence"}
    assert out[0]["content"][1]["type"] == "tool_result"
    assert out[0]["content"][1]["tool_use_id"] == "t1"


def test_attach_retrieval_finds_latest_user_message_among_assistants():
    """If history ends with [user, assistant, user], retrieval lands on the
    second user (the trailing one). Models still receive evidence on the
    just-asked question, not on a stale earlier turn."""
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "ack"},
        {"role": "user", "content": "second"},
    ]
    out = attach_retrieval_to_user_message(messages, "block")
    assert out[0]["content"] == "first"  # untouched
    assert out[1]["content"] == "ack"
    assert out[2]["content"].startswith("block")
    assert out[2]["content"].endswith("second")
