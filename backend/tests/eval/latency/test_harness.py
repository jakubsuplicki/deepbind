"""Unit tests for harness streaming-timing logic (ADR 011).

The harness is exercised against a stubbed httpx client so we can verify
TTFT capture, decode-tps computation from done-event counters, and error
handling without spinning up a real Ollama instance.
"""

from __future__ import annotations

import json
from typing import Iterator

import httpx
import pytest

from tests.eval.latency.harness import (
    AnthropicTimedClient,
    OllamaTimedClient,
    _strip_thinking,
)


def _ndjson_chunks(events: list[dict]) -> Iterator[bytes]:
    for evt in events:
        yield (json.dumps(evt) + "\n").encode("utf-8")


def _make_mock_transport(events: list[dict], *, status: int = 200) -> httpx.MockTransport:
    """Build a MockTransport that streams NDJSON events as a chunked response."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = b"".join(_ndjson_chunks(events))
        return httpx.Response(
            status_code=status,
            content=body,
            headers={"content-type": "application/x-ndjson"},
        )

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_ollama_streaming_captures_ttft_and_decode_tps(monkeypatch):
    """First non-empty content delta is TTFT; done-event eval_count drives tps."""
    # Three "tokens" arriving as chunks, then a done event with eval counters
    events = [
        {"message": {"content": "Hel"}, "done": False},
        {"message": {"content": "lo"}, "done": False},
        {"message": {"content": "!"}, "done": False},
        {
            "message": {"content": ""},
            "done": True,
            "eval_count": 3,
            "eval_duration": 60_000_000,  # 60 ms in nanoseconds → 50 tps
            "prompt_eval_count": 12,
        },
    ]
    transport = _make_mock_transport(events)

    # Patch httpx.AsyncClient to use the mock transport
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched_async_client)

    client = OllamaTimedClient()
    result = await client.call(
        model="qwen3:8b",
        system_prompt="sys",
        user_message="hi",
        max_output_tokens=8,
        seed=1,
        scenario_name="warm-short",
    )

    assert result.error is None, result.error
    assert result.response_text == "Hello!"
    assert result.output_tokens == 3
    assert result.prompt_tokens == 12
    assert result.decode_tps == pytest.approx(50.0, rel=0.01)
    assert result.ttft_ms > 0  # captured something
    assert result.total_ms >= result.ttft_ms


@pytest.mark.asyncio
async def test_ollama_streaming_handles_http_error(monkeypatch):
    """A 500 from Ollama produces an error result, not an exception."""
    transport = _make_mock_transport([], status=500)

    real_async_client = httpx.AsyncClient

    def patched_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched_async_client)

    client = OllamaTimedClient()
    result = await client.call(
        model="qwen3:8b",
        system_prompt="sys",
        user_message="hi",
        max_output_tokens=8,
        scenario_name="warm-short",
    )

    assert result.error is not None
    assert "HTTP 500" in result.error


@pytest.mark.asyncio
async def test_anthropic_skips_silently_without_api_key(monkeypatch):
    """No API key → returns an error TimedResponse rather than raising."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client = AnthropicTimedClient(api_key=None)
    result = await client.call(
        system_prompt="sys",
        user_message="hi",
        max_output_tokens=8,
        scenario_name="reference",
    )
    assert result.error is not None
    assert "ANTHROPIC_API_KEY" in result.error
    assert result.ttft_ms == 0.0


def test_strip_thinking_drops_chain_of_thought():
    raw = "I think the user wants...\n</think>The answer is 42."
    assert _strip_thinking(raw) == "The answer is 42."


def test_strip_thinking_is_noop_without_close_tag():
    raw = "no thinking tag here"
    assert _strip_thinking(raw) == raw
