"""Unit tests for `services.ollama_dispatcher` (ADR 015 chunk 2).

The dispatcher is a pure adapter from `ollama.AsyncClient.chat(stream=True)`
events onto our `StreamEvent` shape. Tests mock the official client so we
exercise the adapter logic in isolation — no live Ollama process required.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import ollama
import pytest
from ollama import ChatResponse, Message

from services.ollama_dispatcher import (
    DEFAULT_KEEP_ALIVE,
    OllamaDispatchConfig,
    OllamaDispatcher,
    convert_messages_anthropic_to_ollama,
    convert_tools_anthropic_to_ollama,
)


pytestmark = pytest.mark.anyio(backends=["asyncio"])


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


# ── Helpers ──────────────────────────────────────────────────────────────────


def _chunk(
    content: str = "",
    *,
    tool_calls=None,
    done: bool = False,
    prompt_eval_count=None,
    eval_count=None,
    eval_duration=None,
    prompt_eval_duration=None,
    load_duration=None,
    total_duration=None,
) -> ChatResponse:
    """Build a single streaming chunk in the ChatResponse shape Ollama emits.

    Per-stage durations are nanoseconds, matching Ollama's wire format.
    They feed the per-turn telemetry surface (ADR 005 §C trigger 2).
    """
    extras = {}
    if eval_duration is not None:
        extras["eval_duration"] = eval_duration
    if prompt_eval_duration is not None:
        extras["prompt_eval_duration"] = prompt_eval_duration
    if load_duration is not None:
        extras["load_duration"] = load_duration
    if total_duration is not None:
        extras["total_duration"] = total_duration
    return ChatResponse(
        model="qwen3:8b",
        created_at="2026-04-30T12:00:00Z",
        message=Message(role="assistant", content=content, tool_calls=tool_calls),
        done=done,
        prompt_eval_count=prompt_eval_count,
        eval_count=eval_count,
        **extras,
    )


def _tool_call(name: str, arguments: dict) -> Message.ToolCall:
    return Message.ToolCall(function=Message.ToolCall.Function(name=name, arguments=arguments))


async def _astream(*chunks: ChatResponse) -> AsyncIterator[ChatResponse]:
    for c in chunks:
        yield c


def _make_dispatcher(**overrides) -> OllamaDispatcher:
    cfg = OllamaDispatchConfig(model=overrides.pop("model", "qwen3:8b"), **overrides)
    return OllamaDispatcher(cfg)


# ── Streaming happy paths ────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_text_only_stream_emits_text_deltas_then_done():
    d = _make_dispatcher()
    chunks = (
        _chunk("Hello"),
        _chunk(", "),
        _chunk("world."),
        _chunk("", done=True, prompt_eval_count=10, eval_count=3),
    )

    with patch.object(d._client, "chat", new=AsyncMock(return_value=_astream(*chunks))):
        events = [e async for e in d.stream_response([], "system", [])]

    types = [e.type for e in events]
    assert types == ["text_delta", "text_delta", "text_delta", "usage", "done"]
    assert "".join(e.content for e in events if e.type == "text_delta") == "Hello, world."
    usage = next(e for e in events if e.type == "usage")
    assert usage.input_tokens == 10 and usage.output_tokens == 3


@pytest.mark.anyio
async def test_tool_call_chunk_synthesizes_id_and_passes_arguments():
    d = _make_dispatcher()
    chunks = (
        _chunk("calling "),
        _chunk("", tool_calls=[_tool_call("search", {"query": "neutrinos"})]),
        _chunk("", done=True, prompt_eval_count=5, eval_count=2),
    )

    with patch.object(d._client, "chat", new=AsyncMock(return_value=_astream(*chunks))):
        events = [e async for e in d.stream_response([], "system", [])]

    tool_events = [e for e in events if e.type == "tool_use"]
    assert len(tool_events) == 1
    te = tool_events[0]
    assert te.name == "search"
    assert te.tool_input == {"query": "neutrinos"}
    assert te.tool_use_id.startswith("toolu_") and len(te.tool_use_id) > len("toolu_")


@pytest.mark.anyio
async def test_multiple_tool_calls_in_one_chunk_each_get_unique_id():
    d = _make_dispatcher()
    chunks = (
        _chunk(
            "",
            tool_calls=[
                _tool_call("search", {"q": "a"}),
                _tool_call("search", {"q": "b"}),
            ],
        ),
        _chunk("", done=True),
    )

    with patch.object(d._client, "chat", new=AsyncMock(return_value=_astream(*chunks))):
        events = [e async for e in d.stream_response([], "system", [])]

    tool_events = [e for e in events if e.type == "tool_use"]
    assert len(tool_events) == 2
    assert tool_events[0].tool_use_id != tool_events[1].tool_use_id
    assert [te.tool_input for te in tool_events] == [{"q": "a"}, {"q": "b"}]


@pytest.mark.anyio
async def test_done_chunk_without_token_counts_does_not_emit_usage():
    d = _make_dispatcher()
    chunks = (
        _chunk("hi"),
        _chunk("", done=True),  # no eval counts
    )

    with patch.object(d._client, "chat", new=AsyncMock(return_value=_astream(*chunks))):
        events = [e async for e in d.stream_response([], "system", [])]

    types = [e.type for e in events]
    assert "usage" not in types
    assert types[-1] == "done"


# ── Per-stage duration forwarding (ADR 005 §C trigger 2) ─────────────────────


@pytest.mark.anyio
async def test_done_chunk_forwards_per_stage_durations_on_usage_event():
    """Ollama's authoritative timings reach the StreamEvent.

    Without these, the chat router can't compute decode_tps and the
    per-turn telemetry / health watcher have no data to surface.
    """
    d = _make_dispatcher()
    chunks = (
        _chunk("hi"),
        _chunk(
            "",
            done=True,
            prompt_eval_count=10,
            eval_count=20,
            eval_duration=1_500_000_000,         # 1.5 s decode
            prompt_eval_duration=80_000_000,      # 80 ms prefill
            load_duration=300_000_000,            # 300 ms cold load
            total_duration=2_000_000_000,         # 2 s end-to-end
        ),
    )

    with patch.object(d._client, "chat", new=AsyncMock(return_value=_astream(*chunks))):
        events = [e async for e in d.stream_response([], "system", [])]

    usage = next(e for e in events if e.type == "usage")
    assert usage.eval_duration_ns == 1_500_000_000
    assert usage.prompt_eval_duration_ns == 80_000_000
    assert usage.load_duration_ns == 300_000_000
    assert usage.total_duration_ns == 2_000_000_000


@pytest.mark.anyio
async def test_done_chunk_without_durations_yields_usage_with_none_durations():
    """Older Ollama versions / partial responses: token counts present,
    durations absent. The dispatcher must emit usage with token counts
    and None durations rather than dropping the event."""
    d = _make_dispatcher()
    chunks = (
        _chunk("hi"),
        _chunk("", done=True, prompt_eval_count=4, eval_count=2),  # no durations
    )

    with patch.object(d._client, "chat", new=AsyncMock(return_value=_astream(*chunks))):
        events = [e async for e in d.stream_response([], "system", [])]

    usage = next(e for e in events if e.type == "usage")
    assert usage.input_tokens == 4 and usage.output_tokens == 2
    assert usage.eval_duration_ns is None
    assert usage.prompt_eval_duration_ns is None
    assert usage.load_duration_ns is None
    assert usage.total_duration_ns is None


# ── chat() call-arg verification ─────────────────────────────────────────────


@pytest.mark.anyio
async def test_chat_called_with_options_keep_alive_and_no_tools_when_empty():
    d = _make_dispatcher(max_tokens=2048, temperature=0.5, keep_alive="15m")
    mock = AsyncMock(return_value=_astream(_chunk("ok"), _chunk("", done=True)))
    with patch.object(d._client, "chat", new=mock):
        async for _ in d.stream_response([], "sys", []):
            pass

    kwargs = mock.call_args.kwargs
    assert kwargs["model"] == "qwen3:8b"
    assert kwargs["stream"] is True
    assert kwargs["keep_alive"] == "15m"
    assert kwargs["options"] == {"num_predict": 2048, "temperature": 0.5}
    # think: False — qwen3-style chain-of-thought suppression at the API
    # level (see dispatcher comment). Without this, message.thinking
    # tokens surface for several seconds before any content delta.
    assert kwargs["think"] is False
    assert "tools" not in kwargs  # empty tools list → not passed


@pytest.mark.anyio
async def test_chat_called_with_converted_tools_when_provided():
    d = _make_dispatcher()
    mock = AsyncMock(return_value=_astream(_chunk("", done=True)))
    with patch.object(d._client, "chat", new=mock):
        async for _ in d.stream_response(
            [],
            "sys",
            [{"name": "search", "description": "Search the web", "input_schema": {"type": "object"}}],
        ):
            pass

    kwargs = mock.call_args.kwargs
    assert kwargs["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "search",
                "description": "Search the web",
                "parameters": {"type": "object"},
            },
        }
    ]


@pytest.mark.anyio
async def test_system_prompt_is_prepended_as_first_message():
    d = _make_dispatcher()
    mock = AsyncMock(return_value=_astream(_chunk("", done=True)))
    with patch.object(d._client, "chat", new=mock):
        async for _ in d.stream_response(
            [{"role": "user", "content": "hi"}], "be helpful", []
        ):
            pass

    msgs = mock.call_args.kwargs["messages"]
    assert msgs[0] == {"role": "system", "content": "be helpful"}
    assert msgs[1] == {"role": "user", "content": "hi"}


# ── Model name normalisation ─────────────────────────────────────────────────


def test_model_name_strips_litellm_prefix():
    d = _make_dispatcher(model="ollama_chat/qwen3:8b")
    assert d._model == "qwen3:8b"


def test_model_name_passthrough_when_no_prefix():
    d = _make_dispatcher(model="qwen3:8b")
    assert d._model == "qwen3:8b"


# ── Error mapping ────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_response_error_becomes_error_event():
    d = _make_dispatcher()
    err = ollama.ResponseError("model not found", status_code=404)

    with patch.object(d._client, "chat", new=AsyncMock(side_effect=err)):
        events = [e async for e in d.stream_response([], "sys", [])]

    assert len(events) == 1
    assert events[0].type == "error"
    assert "model not found" in events[0].content


@pytest.mark.anyio
async def test_request_error_becomes_error_event():
    d = _make_dispatcher()
    with patch.object(d._client, "chat", new=AsyncMock(side_effect=ollama.RequestError("bad request"))):
        events = [e async for e in d.stream_response([], "sys", [])]

    assert events[0].type == "error" and "bad request" in events[0].content


@pytest.mark.anyio
async def test_connect_error_yields_runtime_unreachable_message():
    d = _make_dispatcher()
    with patch.object(d._client, "chat", new=AsyncMock(side_effect=httpx.ConnectError("conn refused"))):
        events = [e async for e in d.stream_response([], "sys", [])]

    assert events[0].type == "error"
    assert "Ollama" in events[0].content


@pytest.mark.anyio
async def test_timeout_yields_timeout_message():
    d = _make_dispatcher()
    with patch.object(d._client, "chat", new=AsyncMock(side_effect=httpx.TimeoutException("timed out"))):
        events = [e async for e in d.stream_response([], "sys", [])]

    assert events[0].type == "error"
    assert "timed out" in events[0].content.lower()


@pytest.mark.anyio
async def test_unexpected_exception_yields_error_event_not_raise():
    d = _make_dispatcher()
    with patch.object(d._client, "chat", new=AsyncMock(side_effect=RuntimeError("boom"))):
        events = [e async for e in d.stream_response([], "sys", [])]

    assert len(events) == 1
    assert events[0].type == "error" and "boom" in events[0].content


# ── Tool format converter ────────────────────────────────────────────────────


def test_convert_tools_renames_input_schema_to_parameters():
    out = convert_tools_anthropic_to_ollama(
        [
            {"name": "a", "description": "A tool", "input_schema": {"type": "object"}},
            {"name": "b", "input_schema": {"type": "object", "properties": {}}},  # no description
        ]
    )
    assert out == [
        {"type": "function", "function": {"name": "a", "description": "A tool", "parameters": {"type": "object"}}},
        {"type": "function", "function": {"name": "b", "description": "", "parameters": {"type": "object", "properties": {}}}},
    ]


# ── Message converter ────────────────────────────────────────────────────────


def test_convert_messages_passes_through_string_content():
    msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    assert convert_messages_anthropic_to_ollama(msgs) == msgs


def test_convert_messages_assistant_with_tool_use_emits_tool_calls_with_dict_arguments():
    msgs = [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "let me search"},
                {"type": "tool_use", "id": "abc", "name": "search", "input": {"query": "x"}},
            ],
        }
    ]
    out = convert_messages_anthropic_to_ollama(msgs)
    assert out == [
        {
            "role": "assistant",
            "content": "let me search",
            "tool_calls": [{"function": {"name": "search", "arguments": {"query": "x"}}}],
        }
    ]


def test_convert_messages_tool_result_blocks_become_tool_role_messages():
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "abc", "content": "found 3 results"},
            ],
        }
    ]
    out = convert_messages_anthropic_to_ollama(msgs)
    assert out == [{"role": "tool", "content": "found 3 results"}]


def test_convert_messages_flattens_block_list_tool_result_content():
    msgs = [
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "abc",
                    "content": [{"type": "text", "text": "line a"}, {"type": "text", "text": "line b"}],
                }
            ],
        }
    ]
    out = convert_messages_anthropic_to_ollama(msgs)
    assert out == [{"role": "tool", "content": "line a\nline b"}]


def test_convert_messages_serialises_non_string_non_list_tool_result_content_as_json():
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "abc", "content": {"hits": 3}},
            ],
        }
    ]
    out = convert_messages_anthropic_to_ollama(msgs)
    assert out == [{"role": "tool", "content": '{"hits": 3}'}]


def test_convert_messages_generic_list_concatenates_text_blocks():
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "hello"},
                {"type": "text", "text": "there"},
            ],
        }
    ]
    out = convert_messages_anthropic_to_ollama(msgs)
    assert out == [{"role": "user", "content": "hello\nthere"}]


def test_convert_messages_handles_empty_assistant_text_with_tool_calls():
    msgs = [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "abc", "name": "search", "input": {}},
            ],
        }
    ]
    out = convert_messages_anthropic_to_ollama(msgs)
    assert out == [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": "search", "arguments": {}}}],
        }
    ]


# ── Defaults ─────────────────────────────────────────────────────────────────


def test_default_keep_alive_is_30m():
    assert DEFAULT_KEEP_ALIVE == "30m"


def test_default_config_uses_module_defaults():
    cfg = OllamaDispatchConfig(model="qwen3:8b")
    assert cfg.max_tokens == 4096
    assert cfg.temperature == 0.7
    assert cfg.timeout == 1800.0
    assert cfg.keep_alive == "30m"
    assert cfg.api_base is None
