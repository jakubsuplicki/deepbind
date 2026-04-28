"""Tests for the chat adapters (ADR 010).

All HTTP / SDK calls are mocked — these tests must NOT require Ollama
running, an Anthropic API key, or network access. Real end-to-end runs
are the responsibility of the (separate) baseline-capture script.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from tests.eval.conversations.chat_adapters import (
    AnthropicChat,
    DEFAULT_ANTHROPIC_MODEL,
    DEFAULT_OLLAMA_BASE_URL,
    DEFAULT_OLLAMA_MODEL,
    OllamaChat,
    _flatten_block_content,
    _split_system_for_anthropic,
    _strip_thinking,
    _translate_messages_for_ollama,
    make_chat,
    make_chat_factory,
)
from tests.eval.conversations.runner import ChatCallable


# ── Protocol satisfaction ────────────────────────────────────────────────────


def test_ollama_chat_satisfies_chat_callable_protocol():
    assert isinstance(OllamaChat(), ChatCallable)


def test_anthropic_chat_satisfies_chat_callable_protocol():
    assert isinstance(AnthropicChat(), ChatCallable)


# ── Defaults ─────────────────────────────────────────────────────────────────


def test_ollama_default_model_matches_adr_008_pinned_chat_slot():
    """ADR 008 pins Qwen3-30B-A3B as the v1 chat slot."""
    chat = OllamaChat()
    assert "qwen3" in chat.model.lower()
    assert "30b" in chat.model.lower() or "a3b" in chat.model.lower()


def test_ollama_default_base_url_is_localhost_loopback():
    """Localhost loopback only — no accidental remote-endpoint default."""
    assert "127.0.0.1" in DEFAULT_OLLAMA_BASE_URL
    assert DEFAULT_OLLAMA_BASE_URL.startswith("http://")


def test_make_chat_defaults_to_ollama():
    """Per project posture: honest measurement of production stack first."""
    chat = make_chat()
    assert isinstance(chat, OllamaChat)


def test_make_chat_anthropic_is_opt_in():
    chat = make_chat("anthropic", api_key="sk-test")
    assert isinstance(chat, AnthropicChat)


def test_make_chat_unknown_provider_raises():
    with pytest.raises(ValueError, match="Unknown chat provider"):
        make_chat("nonsense")


# ── model_id stable identifiers ──────────────────────────────────────────────


def test_ollama_model_id_includes_model_and_seed():
    chat = OllamaChat(model="qwen3:14b", seed=99)
    assert "qwen3:14b" in chat.model_id
    assert "seed=99" in chat.model_id


def test_anthropic_model_id_includes_model():
    chat = AnthropicChat(model="claude-opus-4-7")
    assert "claude-opus-4-7" in chat.model_id


# ── Message-format translation: text-only ────────────────────────────────────


def test_flatten_string_content_returns_unchanged():
    assert _flatten_block_content("hello") == "hello"


def test_flatten_block_list_concatenates_text():
    blocks = [
        {"type": "text", "text": "hello "},
        {"type": "text", "text": "world"},
    ]
    assert _flatten_block_content(blocks) == "hello world"


def test_translate_plain_messages_for_ollama_preserves_strings():
    src = [
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "Hello"},
    ]
    out = _translate_messages_for_ollama(src)
    assert out == [
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "Hello"},
    ]


# ── Message-format translation: tool messages ────────────────────────────────


def test_translate_tool_use_becomes_assistant_with_tool_calls():
    src = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "scripted_read_note_1",
                    "name": "read_note",
                    "input": {"path": "x.md"},
                }
            ],
        }
    ]
    out = _translate_messages_for_ollama(src)
    assert len(out) == 1
    assert out[0]["role"] == "assistant"
    assert "tool_calls" in out[0]
    call = out[0]["tool_calls"][0]
    assert call["function"]["name"] == "read_note"
    assert call["function"]["arguments"] == {"path": "x.md"}


def test_translate_tool_result_becomes_tool_message_with_name_lookup():
    """The tool_result block references its tool_use by id; the translator
    must look up the tool name from the preceding tool_use to populate
    Ollama's ``name`` field."""
    src = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "u1",
                    "name": "read_note",
                    "input": {"path": "x.md"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "u1", "content": "FILE BODY"}
            ],
        },
    ]
    out = _translate_messages_for_ollama(src)
    # 2 messages: assistant (with tool_calls) and tool (the result)
    assert len(out) == 2
    assert out[1]["role"] == "tool"
    assert out[1]["name"] == "read_note"
    assert out[1]["content"] == "FILE BODY"


def test_translate_tool_result_falls_back_to_unknown_when_id_missing():
    """If the fixture is malformed and a tool_result has no preceding
    matching tool_use, the translator must not crash — it falls back to
    'unknown' name. The runner-level orphan check catches the malformed
    fixture earlier; this is just a defensive layer."""
    src = [
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "ghost", "content": "RESULT"}
            ],
        }
    ]
    out = _translate_messages_for_ollama(src)
    assert out[0]["role"] == "tool"
    assert out[0]["name"] == "unknown"
    assert out[0]["content"] == "RESULT"


def test_translate_full_tool_round_trip():
    """End-to-end translation of fixture-#4-shaped messages."""
    src = [
        {"role": "user", "content": "Find all notes about contract X"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "u1", "name": "search_notes", "input": {"q": "X"}}
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "u1", "content": "results..."}
            ],
        },
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "u2", "name": "read_note", "input": {"path": "a.md"}}
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "u2", "content": "file body"}
            ],
        },
        {"role": "assistant", "content": "Synthesis: ..."},
        {"role": "user", "content": "Which file again?"},
    ]
    out = _translate_messages_for_ollama(src)
    roles = [m["role"] for m in out]
    assert roles == ["user", "assistant", "tool", "assistant", "tool", "assistant", "user"]
    # Tool messages carry the right tool name
    tool_messages = [m for m in out if m["role"] == "tool"]
    assert tool_messages[0]["name"] == "search_notes"
    assert tool_messages[1]["name"] == "read_note"


# ── Anthropic system-prompt extraction ───────────────────────────────────────


def test_split_system_for_anthropic_extracts_system_messages():
    msgs = [
        {"role": "system", "content": "You are X."},
        {"role": "user", "content": "Hi"},
        {"role": "system", "content": "Also Y."},
    ]
    sys_str, rest = _split_system_for_anthropic(msgs)
    assert "You are X." in sys_str
    assert "Also Y." in sys_str
    assert all(m["role"] != "system" for m in rest)
    assert len(rest) == 1


def test_split_system_for_anthropic_returns_empty_when_no_system():
    msgs = [{"role": "user", "content": "Hi"}]
    sys_str, rest = _split_system_for_anthropic(msgs)
    assert sys_str == ""
    assert rest == msgs


# ── Thinking-content stripping ───────────────────────────────────────────────


def test_strip_thinking_noop_when_no_close_tag():
    """Anthropic responses and post-stripped Ollama responses must pass through
    unchanged."""
    assert _strip_thinking("just an answer.") == "just an answer."
    assert _strip_thinking("") == ""


def test_strip_thinking_removes_qwen3_chain_of_thought_prefix():
    """Ollama 0.18 + Qwen3 + think:false emits chain-of-thought followed by a
    single </think>, then the real answer. The stripper must keep only the
    real answer."""
    raw = (
        "Okay, let's see. The user is asking for the case number. "
        "Looking back, I think it is I C 1247/23.\n</think>\n\n"
        "Sygnatura sprawy: I C 1247/23."
    )
    assert _strip_thinking(raw) == "Sygnatura sprawy: I C 1247/23."


def test_strip_thinking_takes_text_after_last_close_tag():
    """If a response somehow contains multiple </think> tags, we keep only
    what follows the LAST one — the model has finished thinking by then."""
    raw = "garbage</think>more thinking</think>final answer"
    assert _strip_thinking(raw) == "final answer"


def test_strip_thinking_preserves_response_with_only_close_tag():
    """Even an empty pre-tag region works — strip everything up to and
    including </think>."""
    raw = "</think>just the answer"
    assert _strip_thinking(raw) == "just the answer"


@pytest.mark.asyncio
async def test_ollama_chat_strips_thinking_from_response():
    """End-to-end: Ollama returns chain-of-thought + </think> + answer; the
    adapter returns only the answer to the runner."""
    async def _fake_post(self, url, json=None, **kwargs):
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "message": {
                    "role": "assistant",
                    "content": (
                        "Okay, let me think about this carefully. The user "
                        "wants the function name finalize_invoice, not "
                        "rollback_to_draft.\n</think>\n\n`finalize_invoice`"
                    ),
                }
            },
        )

    with patch.object(httpx.AsyncClient, "post", _fake_post):
        chat = OllamaChat()
        text = await chat([{"role": "user", "content": "x"}], "")

    assert text == "`finalize_invoice`"
    # Crucially, the rejected-candidate name must NOT survive into the
    # returned text — that's exactly the false-positive the strip prevents.
    assert "rollback_to_draft" not in text


# ── OllamaChat HTTP behavior (mocked) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_ollama_chat_posts_to_correct_endpoint_with_pinned_options():
    """The adapter must POST to /api/chat with temperature 0, the seed, and
    the right model. Pinning ensures determinism doesn't silently regress."""
    captured: dict = {}

    async def _fake_post(self, url, json=None, **kwargs):
        captured["url"] = url
        captured["payload"] = json
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"message": {"role": "assistant", "content": "OK"}},
        )

    with patch.object(httpx.AsyncClient, "post", _fake_post):
        chat = OllamaChat(model="qwen3:30b-a3b", seed=42)
        text = await chat([{"role": "user", "content": "ping"}], "sys")

    assert text == "OK"
    assert captured["url"].endswith("/api/chat")
    payload = captured["payload"]
    assert payload["model"] == "qwen3:30b-a3b"
    assert payload["stream"] is False
    assert payload["options"]["temperature"] == 0
    assert payload["options"]["seed"] == 42
    # System prompt prepended verbatim (not augmented)
    assert payload["messages"][0] == {"role": "system", "content": "sys"}
    # Default disable_thinking=True → top-level think:false on the request
    assert payload["think"] is False


@pytest.mark.asyncio
async def test_ollama_chat_omits_think_field_when_thinking_explicitly_enabled():
    """If a developer wants to A/B test with thinking on, disabling the
    default disable_thinking flag must produce a payload with NO `think`
    field at all (matches Ollama default behavior)."""
    captured: dict = {}

    async def _fake_post(self, url, json=None, **kwargs):
        captured["payload"] = json
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"message": {"role": "assistant", "content": "OK"}},
        )

    with patch.object(httpx.AsyncClient, "post", _fake_post):
        chat = OllamaChat(disable_thinking=False)
        await chat([{"role": "user", "content": "ping"}], "sys")

    payload = captured["payload"]
    assert payload["messages"][0] == {"role": "system", "content": "sys"}
    # No top-level "think" field at all when thinking is enabled
    assert "think" not in payload


@pytest.mark.asyncio
async def test_ollama_chat_no_system_message_when_prompt_empty():
    """Empty system_prompt → first message in history is user, NOT a
    fabricated system message. The /no_think directive is now sent via
    the top-level `think` flag, not a synthesized system message."""
    captured: dict = {}

    async def _fake_post(self, url, json=None, **kwargs):
        captured["payload"] = json
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"message": {"role": "assistant", "content": "OK"}},
        )

    with patch.object(httpx.AsyncClient, "post", _fake_post):
        chat = OllamaChat()  # defaults: disable_thinking=True
        await chat([{"role": "user", "content": "ping"}], "")

    payload = captured["payload"]
    # No system message synthesized
    assert payload["messages"][0]["role"] == "user"
    # Thinking still disabled via the top-level flag
    assert payload["think"] is False


def test_ollama_model_id_marks_thinking_explicitly_when_enabled():
    """The ``model_id`` (used in baseline filenames) must distinguish
    thinking-on from thinking-off so an A/B run produces different
    artifacts."""
    chat_default = OllamaChat()
    chat_thinking = OllamaChat(disable_thinking=False)
    assert chat_default.model_id != chat_thinking.model_id
    assert "+think" not in chat_default.model_id  # default has no marker
    assert "+think" in chat_thinking.model_id     # thinking-on is explicitly tagged


@pytest.mark.asyncio
async def test_ollama_chat_includes_num_ctx_when_set():
    captured: dict = {}

    async def _fake_post(self, url, json=None, **kwargs):
        captured["payload"] = json
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"message": {"role": "assistant", "content": "OK"}},
        )

    with patch.object(httpx.AsyncClient, "post", _fake_post):
        chat = OllamaChat(num_ctx=8192)
        await chat([{"role": "user", "content": "ping"}], "")

    assert captured["payload"]["options"]["num_ctx"] == 8192


@pytest.mark.asyncio
async def test_ollama_chat_raises_on_malformed_response():
    """Defensive: if Ollama returns 200 but no message.content, fail loud."""
    async def _fake_post(self, url, json=None, **kwargs):
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"unexpected": "shape"},
        )

    with patch.object(httpx.AsyncClient, "post", _fake_post):
        chat = OllamaChat()
        with pytest.raises(RuntimeError, match="malformed response"):
            await chat([{"role": "user", "content": "x"}], "")


@pytest.mark.asyncio
async def test_ollama_chat_surfaces_http_errors_with_diagnostic_body():
    """When Ollama returns 4xx/5xx, the adapter must surface the response
    body, the model name, and num_ctx in the error — these are the
    parameters the developer needs to diagnose ('500 from Ollama, server
    error' is too vague to be useful)."""
    async def _fake_post(self, url, json=None, **kwargs):
        return httpx.Response(
            500,
            request=httpx.Request("POST", url),
            text="ollama: out of context window",
        )

    with patch.object(httpx.AsyncClient, "post", _fake_post):
        chat = OllamaChat()
        with pytest.raises(RuntimeError) as exc_info:
            await chat([{"role": "user", "content": "x"}], "")

    msg = str(exc_info.value)
    # The error message must include the actionable diagnostic fields
    assert "HTTP 500" in msg
    assert "out of context window" in msg
    assert "num_ctx" in msg
    assert "history_len" in msg


@pytest.mark.asyncio
async def test_ollama_chat_default_num_ctx_is_set_for_real_fixture_sizes():
    """The default num_ctx must be large enough for our 30+ turn fixtures
    to run without context-overflow 500s. 16K is the documented sweet spot."""
    chat = OllamaChat()
    assert chat.num_ctx is not None
    assert chat.num_ctx >= 8192


# ── AnthropicChat behavior (mocked) ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_anthropic_chat_calls_messages_create_with_temperature_zero(monkeypatch):
    """The adapter must call messages.create with temperature 0 and pass the
    system prompt through Anthropic's top-level system parameter."""
    captured: dict = {}

    class _FakeBlock:
        def __init__(self, text):
            self.text = text

    class _FakeResponse:
        content = [_FakeBlock("hello from claude")]

    class _FakeMessages:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return _FakeResponse()

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            self.messages = _FakeMessages()

        async def close(self):
            pass

    monkeypatch.setattr(
        "anthropic.AsyncAnthropic", lambda *args, **kwargs: _FakeClient()
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    chat = AnthropicChat(model="claude-opus-4-7")
    text = await chat([{"role": "user", "content": "ping"}], "system X")

    assert text == "hello from claude"
    assert captured["temperature"] == 0
    assert captured["model"] == "claude-opus-4-7"
    assert "system X" in captured["system"]
    # User message preserved
    assert captured["messages"] == [{"role": "user", "content": "ping"}]


@pytest.mark.asyncio
async def test_anthropic_chat_requires_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    chat = AnthropicChat()
    with pytest.raises(RuntimeError, match="api_key"):
        await chat([{"role": "user", "content": "x"}], "")


@pytest.mark.asyncio
async def test_anthropic_chat_concatenates_multi_block_responses(monkeypatch):
    class _FakeBlock:
        def __init__(self, text):
            self.text = text

    class _FakeResponse:
        content = [_FakeBlock("part one. "), _FakeBlock("part two.")]

    class _FakeMessages:
        async def create(self, **kwargs):
            return _FakeResponse()

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            self.messages = _FakeMessages()

        async def close(self):
            pass

    monkeypatch.setattr(
        "anthropic.AsyncAnthropic", lambda *args, **kwargs: _FakeClient()
    )

    chat = AnthropicChat(api_key="sk-test")
    text = await chat([{"role": "user", "content": "x"}], "sys")
    assert text == "part one. part two."


# ── End-to-end: adapter → runner → scorer with mocked HTTP ───────────────────


@pytest.mark.asyncio
async def test_ollama_adapter_drives_runner_end_to_end():
    """Wire OllamaChat into the runner against a real fixture; mock the HTTP
    call to produce a known-good response. Confirms the adapter produces a
    string the runner and scorer accept end-to-end."""
    from pathlib import Path

    from tests.eval.conversations.runner import load_fixture, run_fixture

    fx = load_fixture(
        Path(__file__).parent / "fixtures" / "01-long-conv-shallow.json"
    )

    async def _fake_post(self, url, json=None, **kwargs):
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "message": {
                    "role": "assistant",
                    "content": (
                        "You mentioned a research project on renewable energy "
                        "adoption in Poland — offshore wind in the Baltic."
                    ),
                }
            },
        )

    from services.chat import FullHistoryStrategy

    with patch.object(httpx.AsyncClient, "post", _fake_post):
        chat = OllamaChat()
        result = await run_fixture(
            fx,
            strategy=FullHistoryStrategy(),
            chat=chat,
            chat_model_id=chat.model_id,
        )

    assert result.mechanical_pass_rate == 1.0
    assert "ollama:" in result.chat_model_id


# ── Chat factory ─────────────────────────────────────────────────────────────


def test_make_chat_factory_returns_callable():
    factory = make_chat_factory("ollama")
    assert callable(factory)


def test_make_chat_factory_produces_seed_specific_adapters():
    factory = make_chat_factory("ollama")
    chat_a = factory(seed=1)
    chat_b = factory(seed=2)
    assert isinstance(chat_a, OllamaChat)
    assert isinstance(chat_b, OllamaChat)
    assert chat_a.seed == 1
    assert chat_b.seed == 2
    # Different model_ids so baseline filenames differ
    assert chat_a.model_id != chat_b.model_id


def test_make_chat_factory_for_anthropic_records_seed_in_model_id():
    """Anthropic doesn't use the seed at the API level, but the factory
    must still embed it in model_id so multi-seed runs produce
    distinguishable per-seed records."""
    factory = make_chat_factory("anthropic", api_key="sk-test")
    chat_a = factory(seed=10)
    chat_b = factory(seed=20)
    assert chat_a.seed == 10
    assert chat_b.seed == 20
    assert chat_a.model_id != chat_b.model_id


def test_make_chat_factory_passes_through_model_override():
    factory = make_chat_factory("ollama", model="qwen3:14b")
    chat = factory(seed=1)
    assert chat.model == "qwen3:14b"
