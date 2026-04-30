"""Direct Ollama dispatcher — sole chat dispatch path per ADR 015.

The dispatcher's job is narrow: adapt the official `ollama.AsyncClient.chat`
streaming API onto the `StreamEvent` shape that the chat router and frontend
listener consume. No multi-provider machinery, no LiteLLM, no cloud SDKs —
single-target local-only dispatch against the bundled Ollama runtime.

Design notes (recorded here so they survive the LiteLLM removal in chunk 4):

* **Tool-call IDs are synthesized.** Ollama's wire format does not carry an
  `id` field on tool calls (only `function.name` + `function.arguments`).
  The dispatcher mints a stable `tool_use_id` at emit time so downstream
  `tool_use` ↔ `tool_result` correlation in the frontend remains intact.
  When `tool_result` blocks come back as messages, the converter discards
  the (now-meaningless to Ollama) id and relies on positional ordering of
  tool messages — the convention Ollama expects.

* **Tool-call arguments arrive complete, not streamed.** Unlike LiteLLM
  (which streams partial JSON for tool args), the official Ollama client
  emits already-parsed `Mapping[str, Any]` arguments per chunk. No
  accumulator needed — emit the `tool_use` event the moment the chunk
  carrying it arrives.

* **Errors are mapped at the adapter boundary.** `ollama.RequestError` and
  `ollama.ResponseError` cover wire-protocol failures; `httpx.ConnectError`
  / `httpx.TimeoutException` cover transport. All become a single
  `StreamEvent(type="error", content=...)` with copy tuned for the local
  runtime (no "API key" hints — there is no API key).
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Optional

import httpx
import ollama

from services.system_prompt import StreamEvent

logger = logging.getLogger(__name__)


# ── Defaults ─────────────────────────────────────────────────────────────────

# Match the existing LiteLLM-path defaults so chunk 3's wire-up is mechanical.
DEFAULT_MODEL = "qwen3:8b"
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TEMPERATURE = 0.7
DEFAULT_TIMEOUT_SECONDS = 1800.0  # local first-load can be slow on CPU
DEFAULT_KEEP_ALIVE = "30m"


@dataclass
class OllamaDispatchConfig:
    """Per-request Ollama configuration.

    Mirrors the shape `LLMConfig` exposed for the cloud era, minus
    the provider/api_key fields that have no meaning for local-only.
    """

    model: str  # e.g. "qwen3:8b" — `ollama_chat/` prefix is stripped if present
    api_base: Optional[str] = None  # default: services.ollama_service.DEFAULT_OLLAMA_BASE_URL
    max_tokens: int = DEFAULT_MAX_TOKENS
    temperature: float = DEFAULT_TEMPERATURE
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    keep_alive: str = DEFAULT_KEEP_ALIVE


def _strip_litellm_prefix(model: str) -> str:
    """Accept `ollama_chat/qwen3:8b` or `qwen3:8b` — strip the LiteLLM-era prefix."""
    return model.removeprefix("ollama_chat/")


def _synthesize_tool_call_id() -> str:
    """Mint a tool_use_id for a tool call Ollama does not assign one to."""
    return f"toolu_{uuid.uuid4().hex[:24]}"


# ── Anthropic-style ↔ Ollama-style format converters ─────────────────────────

def convert_tools_anthropic_to_ollama(anthropic_tools: list[dict]) -> list[dict]:
    """Anthropic `{name, description, input_schema}` → Ollama `{type, function: {...}}`.

    The shape is identical to the OpenAI/LiteLLM tool format because Ollama
    adopted that convention; the conversion is a rename of `input_schema`
    to `parameters`.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {}),
            },
        }
        for t in anthropic_tools
    ]


def convert_messages_anthropic_to_ollama(messages: list[dict]) -> list[dict]:
    """Convert chat-router-shape messages to the format Ollama expects.

    Handles the three content shapes the rest of the codebase produces:

    * Plain string content → pass through.
    * Assistant turn with `tool_use` blocks → assistant message with
      `tool_calls` array; arguments are emitted as already-parsed dicts
      (Ollama does not expect JSON-string arguments the way OpenAI does).
    * `tool_result` blocks → individual `{role: "tool", content: ...}`
      messages, ordered as they appear (Ollama correlates by position
      within the turn, not by id).
    * Anything else with list content → join the text-block contents.
    """
    converted: list[dict] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content")

        if isinstance(content, str):
            converted.append({"role": role, "content": content})
            continue

        if isinstance(content, list):
            block_types = {b.get("type") for b in content}

            if "tool_use" in block_types and role == "assistant":
                tool_calls = []
                text_parts: list[str] = []
                for block in content:
                    if block["type"] == "tool_use":
                        tool_calls.append(
                            {
                                "function": {
                                    "name": block["name"],
                                    "arguments": block.get("input", {}),
                                }
                            }
                        )
                    elif block["type"] == "text":
                        text_parts.append(block.get("text", ""))
                converted.append(
                    {
                        "role": "assistant",
                        "content": "\n".join(text_parts),
                        "tool_calls": tool_calls,
                    }
                )
                continue

            if "tool_result" in block_types:
                for block in content:
                    if block["type"] == "tool_result":
                        result_content = block.get("content", "")
                        # Anthropic allows tool_result content to be a list of blocks;
                        # Ollama wants a plain string. Flatten if needed.
                        if isinstance(result_content, list):
                            result_content = "\n".join(
                                b.get("text", "") if isinstance(b, dict) else str(b)
                                for b in result_content
                            )
                        elif not isinstance(result_content, str):
                            result_content = json.dumps(result_content)
                        converted.append({"role": "tool", "content": result_content})
                continue

            # Generic list — concatenate text blocks.
            text = "\n".join(
                b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
            )
            converted.append({"role": role, "content": text})
            continue

        converted.append({"role": role, "content": str(content) if content is not None else ""})

    return converted


# ── Dispatcher ───────────────────────────────────────────────────────────────


class OllamaDispatcher:
    """Streams a single chat turn against the local Ollama runtime.

    Yields the same `StreamEvent` objects the chat router has always
    consumed: text deltas, tool_use events, a final usage event, and
    a `done` sentinel. Errors are mapped to a `StreamEvent(type="error")`
    so the router can surface a single uniform error path.
    """

    def __init__(self, config: OllamaDispatchConfig):
        self.config = config
        self._model = _strip_litellm_prefix(config.model)
        host = config.api_base
        if host is None:
            from services.ollama_service import DEFAULT_OLLAMA_BASE_URL
            host = DEFAULT_OLLAMA_BASE_URL
        # The official client wraps an httpx.AsyncClient; the timeout we pass
        # here governs every per-call HTTP timeout.
        self._client = ollama.AsyncClient(host=host, timeout=httpx.Timeout(config.timeout))

    async def stream_response(
        self,
        messages: list[dict],
        system_prompt: str,
        tools: list[dict],
    ) -> AsyncIterator[StreamEvent]:
        """Stream a single chat turn — interface-compatible with `LLMService.stream_response`.

        Note: `system_prompt` is prepended as a `{role: "system"}` message.
        Ollama supports first-class system messages this way; there is no
        separate `system=` parameter to feed it through.
        """
        try:
            ollama_messages = [{"role": "system", "content": system_prompt}]
            ollama_messages.extend(convert_messages_anthropic_to_ollama(messages))

            kwargs: dict[str, Any] = {
                "model": self._model,
                "messages": ollama_messages,
                "stream": True,
                "keep_alive": self.config.keep_alive,
                "options": {
                    "num_predict": self.config.max_tokens,
                    "temperature": self.config.temperature,
                },
            }

            if tools:
                kwargs["tools"] = convert_tools_anthropic_to_ollama(tools)

            response = await self._client.chat(**kwargs)

            async for chunk in response:
                # Text content arrives as incremental deltas in `message.content`.
                if chunk.message.content:
                    yield StreamEvent(type="text_delta", content=chunk.message.content)

                # Tool calls arrive complete (no streamed args). Emit one
                # `tool_use` event per call with a synthesized id.
                if chunk.message.tool_calls:
                    for call in chunk.message.tool_calls:
                        yield StreamEvent(
                            type="tool_use",
                            name=call.function.name,
                            tool_input=dict(call.function.arguments or {}),
                            tool_use_id=_synthesize_tool_call_id(),
                        )

                # Final chunk carries `done=True` plus authoritative token counts.
                if chunk.done and (chunk.prompt_eval_count is not None or chunk.eval_count is not None):
                    yield StreamEvent(
                        type="usage",
                        input_tokens=chunk.prompt_eval_count or 0,
                        output_tokens=chunk.eval_count or 0,
                    )

            yield StreamEvent(type="done")

        except ollama.ResponseError as exc:
            # Ollama's typed error — usually surfaces "model not found",
            # "out of memory", or a permission/path issue from the server.
            logger.warning("Ollama response error %s: %s", getattr(exc, "status_code", "?"), exc)
            yield StreamEvent(type="error", content=str(exc)[:300])
        except ollama.RequestError as exc:
            logger.warning("Ollama request error: %s", exc)
            yield StreamEvent(type="error", content=str(exc)[:300])
        except (httpx.ConnectError, httpx.RemoteProtocolError) as exc:
            logger.warning("Cannot reach Ollama runtime: %s", exc)
            yield StreamEvent(
                type="error",
                content="Cannot reach the local model runtime. Is Ollama running?",
            )
        except httpx.TimeoutException as exc:
            logger.warning("Ollama request timed out: %s", exc)
            yield StreamEvent(
                type="error",
                content="Request timed out. The model may be loading or under heavy load.",
            )
        except Exception as exc:  # noqa: BLE001 — last-resort guard
            logger.exception("Unexpected Ollama error")
            yield StreamEvent(type="error", content=f"Unexpected error: {str(exc)[:200]}")

    async def close(self) -> None:
        await self._client.close()
