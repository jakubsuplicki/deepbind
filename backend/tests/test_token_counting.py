"""Tests for the ADR 009 token-counting wrapper.

The wrapper has two modes that both have to behave correctly: the HF
tokenizer path and the char/4 fallback. Real users hit the tokenizer
path; tests run with ``HF_HUB_OFFLINE=1`` (set in this module) so they
exercise the fallback path deterministically without touching the
network. A handful of tests use a stub Tokenizer to verify the cache
and message-flattening contracts on the tokenizer-present path.
"""

from __future__ import annotations

import os

# Force the offline path before importing the module so its lazy load
# never tries to reach huggingface.co during the test run. Tests that
# want to exercise the loaded-tokenizer path inject a stub directly into
# the module's cache and never call _load_tokenizer.
os.environ["HF_HUB_OFFLINE"] = "1"

import pytest

from services import token_counting


@pytest.fixture(autouse=True)
def _reset_cache():
    token_counting.reset_cache_for_tests()
    yield
    token_counting.reset_cache_for_tests()


# ── Fallback path (no tokenizer) ──────────────────────────────────────


def test_count_tokens_empty_string_is_zero():
    assert token_counting.count_tokens("") == 0


def test_count_tokens_none_id_uses_fallback():
    # ~12 chars / 4 ≈ 3 tokens
    assert token_counting.count_tokens("hello world!", tokenizer_id=None) == 3


def test_count_tokens_unknown_id_falls_back_when_offline():
    """An id that doesn't load (offline / missing pkg) returns the char/4 estimate."""
    text = "x" * 40
    assert token_counting.count_tokens(text, tokenizer_id="not-a-real-id") == 10


def test_count_tokens_floor_one_on_short_text():
    # Even a single-char text should report at least 1 token, not 0,
    # so the budget math doesn't divide-by-zero.
    assert token_counting.count_tokens("a") == 1


def test_failed_load_is_sticky_across_calls():
    """The second call must not retry the failed load."""
    calls: list[str] = []

    def _spy_load(tokenizer_id: str):
        calls.append(tokenizer_id)
        return None

    original = token_counting._load_tokenizer
    token_counting._load_tokenizer = _spy_load  # type: ignore[assignment]
    try:
        token_counting.count_tokens("hello", tokenizer_id="bogus-tokenizer")
        token_counting.count_tokens("hello again", tokenizer_id="bogus-tokenizer")
        assert calls == ["bogus-tokenizer"]  # Only attempted once
    finally:
        token_counting._load_tokenizer = original  # type: ignore[assignment]


# ── Tokenizer-present path (stubbed) ──────────────────────────────────


class _StubEncoding:
    def __init__(self, ids: list[int]):
        self.ids = ids


class _StubTokenizer:
    def __init__(self, tokens_per_char: float = 0.5):
        self._tpc = tokens_per_char

    def encode(self, text: str, *, add_special_tokens: bool = False):
        # Deterministic count proportional to length so tests can assert
        # exact numbers without depending on a real tokenizer.
        return _StubEncoding([0] * max(0, int(len(text) * self._tpc)))


def _inject_stub(tokenizer_id: str, stub: _StubTokenizer) -> None:
    token_counting._TOKENIZER_CACHE[tokenizer_id] = stub


def test_count_tokens_uses_loaded_tokenizer():
    _inject_stub("Qwen/Qwen3-8B-stub", _StubTokenizer(tokens_per_char=0.5))
    # 20 chars × 0.5 = 10 tokens
    assert token_counting.count_tokens("a" * 20, tokenizer_id="Qwen/Qwen3-8B-stub") == 10


def test_cache_serves_subsequent_calls():
    """A pre-populated cache entry must be returned without re-loading."""
    stub = _StubTokenizer(tokens_per_char=0.25)
    _inject_stub("Qwen/cached", stub)
    assert token_counting.get_tokenizer("Qwen/cached") is stub
    # Second call returns the same instance.
    assert token_counting.get_tokenizer("Qwen/cached") is stub


def test_tokenizer_failure_is_caught_during_count():
    """If encode() blows up, fall back to char/4 for just that call."""
    class _ExplodingTokenizer:
        def encode(self, text, *, add_special_tokens=False):
            raise RuntimeError("BOOM")

    _inject_stub("Qwen/explode", _ExplodingTokenizer())
    # 16 chars → fallback gives 4 tokens
    assert token_counting.count_tokens("x" * 16, tokenizer_id="Qwen/explode") == 4


# ── Message flattening / counting ─────────────────────────────────────


def test_count_message_tokens_string_content():
    _inject_stub("Qwen/m", _StubTokenizer(tokens_per_char=1.0))
    msg = {"role": "user", "content": "hello"}
    assert token_counting.count_message_tokens(msg, tokenizer_id="Qwen/m") == 5


def test_count_message_tokens_block_text_only():
    _inject_stub("Qwen/m", _StubTokenizer(tokens_per_char=1.0))
    msg = {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "hi"},
            {"type": "text", "text": "there"},
        ],
    }
    # "hi there" → 8 chars × 1 token/char = 8 tokens
    assert token_counting.count_message_tokens(msg, tokenizer_id="Qwen/m") == 8


def test_count_message_tokens_tool_use_includes_input_json():
    _inject_stub("Qwen/m", _StubTokenizer(tokens_per_char=1.0))
    msg = {
        "role": "assistant",
        "content": [
            {"type": "tool_use", "id": "t1", "name": "read_note", "input": {"path": "x.md"}},
        ],
    }
    n = token_counting.count_message_tokens(msg, tokenizer_id="Qwen/m")
    assert n > 0
    assert n >= len("read_note")


def test_count_message_tokens_tool_result_string_payload():
    _inject_stub("Qwen/m", _StubTokenizer(tokens_per_char=1.0))
    payload = "result body" * 3
    msg = {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": payload},
        ],
    }
    assert token_counting.count_message_tokens(msg, tokenizer_id="Qwen/m") == len(payload)


def test_count_messages_tokens_sums_messages():
    _inject_stub("Qwen/m", _StubTokenizer(tokens_per_char=1.0))
    msgs = [
        {"role": "user", "content": "abc"},
        {"role": "assistant", "content": "defgh"},
    ]
    # 3 + 5 = 8
    assert token_counting.count_messages_tokens(msgs, tokenizer_id="Qwen/m") == 8


def test_count_messages_tokens_empty_list_is_zero():
    assert token_counting.count_messages_tokens([], tokenizer_id=None) == 0
