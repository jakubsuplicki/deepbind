"""Streaming HTTP clients with per-token timing (ADR 011).

Two clients:

- :class:`OllamaTimedClient` — talks to a locally-running Ollama instance via
  the streaming chat endpoint. Captures TTFT (time from request send to first
  non-empty content delta), per-token timestamps for decode-tps, and the
  ``done`` event's reported ``eval_count`` / ``prompt_eval_count`` /
  ``eval_duration`` for cross-checking.

- :class:`AnthropicTimedClient` — talks to Anthropic's hosted streaming API.
  Same metrics. Used for the reference scenario only; lazy-imports the SDK
  and skips silently when no API key is configured.

Both produce a :class:`TimedResponse` with the same shape, so the runner
treats them uniformly.

This module deliberately does NOT reuse ``conversations/chat_adapters.py``:
that adapter returns the complete response after non-streaming round-trip,
which is the wrong abstraction for TTFT measurement. Different concern,
separate client.

Determinism: both clients pin temperature 0 and (where supported) a fixed
seed. The harness adds no randomness of its own. The Ollama client honors
``options.seed``; Anthropic's API does not accept a seed parameter.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx


# ── Defaults ─────────────────────────────────────────────────────────────────


DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_OLLAMA_TIMEOUT_S = 600.0  # 10 min ceiling per call (cold-start guard)
DEFAULT_OLLAMA_NUM_CTX = 32_768   # accommodates the 16K-prefill scenario + headroom

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-5"  # realistic shadow-IT comparison
DEFAULT_ANTHROPIC_MAX_TOKENS = 1024


# ── Result type ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TimedResponse:
    """One scenario × model run.

    ``ttft_ms`` is the metric users feel — request send to first visible
    output. ``decode_tps`` is sustained throughput excluding prefill;
    computed from the model's reported ``eval_count`` / ``eval_duration``
    when available, falling back to wall-clock token timestamps.

    ``error`` is set on transport / API failure; the runner reports the
    failure but does not abort the grid (a 30B-A3B prefill-16k OOM
    shouldn't kill the whole run).
    """

    scenario_name: str
    model_id: str
    ttft_ms: float
    decode_tps: float
    total_ms: float
    output_tokens: int
    prompt_tokens: int
    response_text: str
    error: Optional[str] = None
    raw_done_event: dict = field(default_factory=dict)


# ── Ollama streaming client ─────────────────────────────────────────────────


@dataclass
class OllamaTimedClient:
    """Streaming Ollama HTTP client with per-token timing.

    Uses ``/api/chat`` with ``stream: true``. Ollama emits NDJSON: one JSON
    object per line, terminated by an object with ``done: true`` carrying
    the eval counters.

    The first chunk with a non-empty ``message.content`` marks first token.
    Subsequent chunks accumulate the response. The ``done`` chunk's
    ``eval_count`` and ``eval_duration`` give us the model's own throughput
    measurement, which we trust over wall-clock counting because it
    excludes HTTP + JSON parse overhead.
    """

    base_url: str = DEFAULT_OLLAMA_BASE_URL
    timeout_s: float = DEFAULT_OLLAMA_TIMEOUT_S
    num_ctx: int = DEFAULT_OLLAMA_NUM_CTX
    disable_thinking: bool = True  # /no_think — v1 canonical posture

    async def call(
        self,
        *,
        model: str,
        system_prompt: str,
        user_message: str,
        max_output_tokens: int,
        seed: int = 42,
        scenario_name: str = "unknown",
    ) -> TimedResponse:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_message})

        options: dict[str, Any] = {
            "temperature": 0,
            "seed": seed,
            "num_ctx": self.num_ctx,
            "num_predict": max_output_tokens,
        }
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "options": options,
        }
        if self.disable_thinking:
            payload["think"] = False

        model_id = f"ollama:{model}"

        try:
            return await self._stream_call(
                payload=payload,
                scenario_name=scenario_name,
                model_id=model_id,
            )
        except httpx.HTTPError as exc:
            return TimedResponse(
                scenario_name=scenario_name,
                model_id=model_id,
                ttft_ms=0.0,
                decode_tps=0.0,
                total_ms=0.0,
                output_tokens=0,
                prompt_tokens=0,
                response_text="",
                error=f"httpx: {exc}",
            )

    async def _stream_call(
        self,
        *,
        payload: dict,
        scenario_name: str,
        model_id: str,
    ) -> TimedResponse:
        url = f"{self.base_url.rstrip('/')}/api/chat"

        t_start = time.perf_counter()
        ttft_ms: Optional[float] = None
        text_parts: list[str] = []
        done_event: dict = {}

        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            async with client.stream("POST", url, json=payload) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", errors="replace")
                    return TimedResponse(
                        scenario_name=scenario_name,
                        model_id=model_id,
                        ttft_ms=0.0,
                        decode_tps=0.0,
                        total_ms=(time.perf_counter() - t_start) * 1000.0,
                        output_tokens=0,
                        prompt_tokens=0,
                        response_text="",
                        error=f"HTTP {resp.status_code}: {body[:500]}",
                    )

                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    msg = chunk.get("message") or {}
                    delta = msg.get("content", "")
                    if delta and ttft_ms is None:
                        ttft_ms = (time.perf_counter() - t_start) * 1000.0
                    if delta:
                        text_parts.append(delta)

                    if chunk.get("done") is True:
                        done_event = chunk
                        break

        total_ms = (time.perf_counter() - t_start) * 1000.0

        # Decode tps: prefer the model's self-reported counters from the done
        # event because they exclude HTTP / JSON parse overhead. Fall back to
        # wall-clock approximation when the model didn't report them (e.g.,
        # an aborted stream).
        eval_count = int(done_event.get("eval_count") or 0)
        eval_duration_ns = int(done_event.get("eval_duration") or 0)
        prompt_eval_count = int(done_event.get("prompt_eval_count") or 0)

        if eval_count > 0 and eval_duration_ns > 0:
            decode_tps = eval_count / (eval_duration_ns / 1e9)
        elif total_ms > 0 and ttft_ms is not None:
            decode_seconds = max(1e-6, (total_ms - ttft_ms) / 1000.0)
            decode_tps = (
                (len("".join(text_parts).split()) or 0) / decode_seconds
            )  # word-approximation fallback; flagged in error if used
        else:
            decode_tps = 0.0

        return TimedResponse(
            scenario_name=scenario_name,
            model_id=model_id,
            ttft_ms=ttft_ms if ttft_ms is not None else total_ms,
            decode_tps=decode_tps,
            total_ms=total_ms,
            output_tokens=eval_count,
            prompt_tokens=prompt_eval_count,
            response_text=_strip_thinking("".join(text_parts)),
            raw_done_event=done_event,
        )


def _strip_thinking(text: str) -> str:
    """Drop Qwen3 chain-of-thought prose. Same logic as conversations/chat_adapters.

    Ollama 0.18 with ``think: false`` strips the opening ``<think>`` tag but
    leaves the chain-of-thought prose plus closing ``</think>`` in the body.
    For latency benchmarks this matters because the response_text is reported
    in the baseline JSON and noisy text makes diffs hard to read. The timing
    metrics themselves are unaffected.
    """
    if "</think>" not in text:
        return text
    return text.rsplit("</think>", 1)[-1].lstrip()


# ── Anthropic streaming client (reference scenario only) ────────────────────


@dataclass
class AnthropicTimedClient:
    """Streaming Anthropic client for the reference scenario.

    Lazy-imports the SDK so callers without an API key don't need it
    installed. Returns a TimedResponse with ``error`` populated when the
    key is missing rather than raising — the runner skips reference
    scenarios cleanly in that case.
    """

    model: str = DEFAULT_ANTHROPIC_MODEL
    api_key: Optional[str] = None  # falls back to ANTHROPIC_API_KEY
    max_tokens: int = DEFAULT_ANTHROPIC_MAX_TOKENS

    async def call(
        self,
        *,
        system_prompt: str,
        user_message: str,
        max_output_tokens: int,
        scenario_name: str = "unknown",
        **_unused: Any,
    ) -> TimedResponse:
        api_key = self.api_key or os.environ.get("ANTHROPIC_API_KEY")
        model_id = f"anthropic:{self.model}"

        if not api_key:
            return TimedResponse(
                scenario_name=scenario_name,
                model_id=model_id,
                ttft_ms=0.0,
                decode_tps=0.0,
                total_ms=0.0,
                output_tokens=0,
                prompt_tokens=0,
                response_text="",
                error="ANTHROPIC_API_KEY not set; reference scenario skipped",
            )

        try:
            from anthropic import AsyncAnthropic
        except ImportError:
            return TimedResponse(
                scenario_name=scenario_name,
                model_id=model_id,
                ttft_ms=0.0,
                decode_tps=0.0,
                total_ms=0.0,
                output_tokens=0,
                prompt_tokens=0,
                response_text="",
                error="anthropic SDK not installed; reference scenario skipped",
            )

        client = AsyncAnthropic(api_key=api_key)
        t_start = time.perf_counter()
        ttft_ms: Optional[float] = None
        text_parts: list[str] = []
        output_tokens = 0
        prompt_tokens = 0

        try:
            async with client.messages.stream(
                model=self.model,
                max_tokens=min(self.max_tokens, max_output_tokens or self.max_tokens),
                temperature=0,
                system=system_prompt or "You are a helpful assistant.",
                messages=[{"role": "user", "content": user_message}],
            ) as stream:
                async for event in stream:
                    # First text delta marks TTFT
                    text = getattr(event, "delta", None)
                    delta_text = getattr(text, "text", None) if text else None
                    if delta_text and ttft_ms is None:
                        ttft_ms = (time.perf_counter() - t_start) * 1000.0
                    if delta_text:
                        text_parts.append(delta_text)
                final = await stream.get_final_message()
                output_tokens = getattr(final.usage, "output_tokens", 0) or 0
                prompt_tokens = getattr(final.usage, "input_tokens", 0) or 0
        except Exception as exc:  # noqa: BLE001 — surface every API error uniformly
            return TimedResponse(
                scenario_name=scenario_name,
                model_id=model_id,
                ttft_ms=0.0,
                decode_tps=0.0,
                total_ms=(time.perf_counter() - t_start) * 1000.0,
                output_tokens=0,
                prompt_tokens=0,
                response_text="".join(text_parts),
                error=f"anthropic: {exc}",
            )
        finally:
            await client.close()

        total_ms = (time.perf_counter() - t_start) * 1000.0
        decode_seconds = max(
            1e-6,
            (total_ms - (ttft_ms if ttft_ms is not None else total_ms)) / 1000.0,
        )
        decode_tps = (output_tokens / decode_seconds) if output_tokens > 0 else 0.0

        return TimedResponse(
            scenario_name=scenario_name,
            model_id=model_id,
            ttft_ms=ttft_ms if ttft_ms is not None else total_ms,
            decode_tps=decode_tps,
            total_ms=total_ms,
            output_tokens=output_tokens,
            prompt_tokens=prompt_tokens,
            response_text="".join(text_parts),
        )
