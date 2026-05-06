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


# ── Bundled-tokenizer offline path (audit finding #7) ────────────────


def test_allowlist_matches_catalog():
    """The bundled-tokenizer allowlist must equal the set of tokenizer_id
    values present in MODEL_CATALOG. Drift means a new catalog entry
    silently downgrades to char/4 in production — that's a regression
    worth catching at test time, not at first prod use.
    """
    from services.ollama_service import MODEL_CATALOG

    catalog_ids = {entry.tokenizer_id for entry in MODEL_CATALOG if entry.tokenizer_id}
    assert token_counting._BUNDLED_TOKENIZER_IDS == catalog_ids, (
        "bundled-tokenizer allowlist drift vs MODEL_CATALOG\n"
        f"  in allowlist not in catalog: {sorted(token_counting._BUNDLED_TOKENIZER_IDS - catalog_ids)}\n"
        f"  in catalog not in allowlist: {sorted(catalog_ids - token_counting._BUNDLED_TOKENIZER_IDS)}"
    )


def test_off_allowlist_id_returns_none(tmp_path, monkeypatch):
    """An id not in the allowlist must return None even if a tokenizer.json
    file exists at the resolved path. The allowlist is the gate, not the
    filesystem.
    """
    sanitized_dir = tmp_path / "_bundled_tokenizers" / "evil__model"
    sanitized_dir.mkdir(parents=True)
    (sanitized_dir / "tokenizer.json").write_text("{}")
    monkeypatch.setattr(
        token_counting, "_bundled_tokenizers_root",
        lambda: tmp_path / "_bundled_tokenizers",
    )
    assert token_counting._bundled_tokenizer_path("evil/model") is None


def test_bundled_path_resolution_returns_existing_file(tmp_path, monkeypatch):
    """For an allowlisted id with a bundled tokenizer.json on disk, the
    helper returns the resolved Path. The Tokenizer.from_file caller can
    then load it.
    """
    monkeypatch.setattr(
        token_counting, "_bundled_tokenizers_root",
        lambda: tmp_path,
    )
    sanitized_dir = tmp_path / "Qwen__Qwen3-8B"
    sanitized_dir.mkdir(parents=True)
    expected = sanitized_dir / "tokenizer.json"
    expected.write_text("{}")
    assert token_counting._bundled_tokenizer_path("Qwen/Qwen3-8B") == expected


def test_bundled_path_resolution_returns_none_when_file_missing(tmp_path, monkeypatch):
    """Allowlisted id but missing bundled file → None. Caller falls back
    to char/4. This is the dev-environment behaviour before the user has
    run fetch-bundled-tokenizers.sh; production builds always ship the
    files (the spec aborts the build if the cache is missing).
    """
    monkeypatch.setattr(
        token_counting, "_bundled_tokenizers_root",
        lambda: tmp_path,
    )
    assert token_counting._bundled_tokenizer_path("Qwen/Qwen3-8B") is None


def test_sanitized_id_strips_slash():
    """The HF org/name pair maps to a single directory name via slash
    replacement. Pinned because a future change to use os.sep or '/' as
    a literal directory separator would silently break frozen-bundle
    layouts on Windows.
    """
    assert token_counting._sanitized_id("Qwen/Qwen3-8B") == "Qwen__Qwen3-8B"
    assert token_counting._sanitized_id("ibm-granite/granite-4.0-h-micro") == "ibm-granite__granite-4.0-h-micro"
    assert token_counting._sanitized_id("openai/gpt-oss-120b") == "openai__gpt-oss-120b"


def test_load_tokenizer_never_calls_from_pretrained(tmp_path, monkeypatch):
    """ADR-002 contract: tokenizer loading is bundle-only; from_pretrained
    is the network path and must never be invoked at runtime. This test
    pins the contract so a future PR that "just adds a fallback" trips
    here.

    The test uses an empty tmp_path as the bundled root (instead of the
    real `backend/_bundled_tokenizers/` which may or may not be populated
    on the dev machine) so the path-existence check returns None for
    every id and we can prove from_pretrained is never reached.
    """
    monkeypatch.setattr(
        token_counting, "_bundled_tokenizers_root",
        lambda: tmp_path,
    )

    calls: list[str] = []

    class _SpyTokenizer:
        @staticmethod
        def from_pretrained(tokenizer_id: str):
            calls.append(f"from_pretrained:{tokenizer_id}")
            raise AssertionError("from_pretrained must not be called — bundle-only contract")

        @staticmethod
        def from_file(path: str):
            calls.append(f"from_file:{path}")
            return object()  # opaque sentinel

    import sys as _sys
    fake_module = type(_sys)("tokenizers")
    fake_module.Tokenizer = _SpyTokenizer
    monkeypatch.setitem(_sys.modules, "tokenizers", fake_module)

    # Off-allowlist: short-circuits at the allowlist gate, never imports.
    assert token_counting._load_tokenizer("evil/model") is None
    assert calls == []

    # On-allowlist but no file on disk (tmp_path is empty): short-circuits
    # at the path-existence check, never invokes Tokenizer at all.
    assert token_counting._load_tokenizer("Qwen/Qwen3-8B") is None
    assert calls == []


def test_missing_bundled_file_falls_back_to_char_quarter():
    """End-to-end: the dev environment without fetched tokenizers must
    keep returning char/4 estimates. No exception, no log spam beyond a
    one-shot warning.
    """
    # Qwen/Qwen3-8B is allowlisted but the dev path may or may not have
    # the file. Either way, count_tokens must succeed and never raise.
    n = token_counting.count_tokens("hello world", tokenizer_id="Qwen/Qwen3-8B")
    assert n > 0  # non-zero; either the real tokenizer count or the char/4 fallback
