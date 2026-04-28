"""ChatCallable adapters for the conversation-replay runner (ADR 010).

Two implementations of the ``ChatCallable`` Protocol:

- :class:`OllamaChat` — talks to a locally-running Ollama instance. The
  default for the eval, since it measures the production stack honestly
  (the same models customers run, the same quantization, the same runtime).
  Slower per turn (~5–15 tok/s on 24 GB unified memory) but the numbers
  reflect what we actually ship.

- :class:`AnthropicChat` — opt-in. Talks to Anthropic's hosted API.
  Faster per turn, costs money per call, and uses a different model
  family than production. Useful for quick iteration on the harness or
  when local hardware can't run the chat-under-test model. Results from
  this adapter must NOT be promoted to the canonical baseline; use it
  for development loops only.

Determinism: both adapters pin temperature to 0 and (where supported) a
fixed seed. Anthropic's API does not accept a seed parameter — its
non-determinism floor is small at temperature 0 but not zero. Ollama
honors ``options.seed`` on most models. Document the caveat per result.

Message-format translation: Anthropic-style messages with
``content`` as a list of blocks (text / tool_use / tool_result) are the
runner's canonical format. Each adapter translates to the provider's
native shape at the boundary. Tool_use / tool_result blocks are honored
because some launch fixtures (#2, #4, #7) script tool interactions in
the assistant turn history that precedes the assistant_target turn.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Optional

import httpx


# ── Defaults ─────────────────────────────────────────────────────────────────


DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_OLLAMA_MODEL = "qwen3:30b-a3b"  # ADR 008's pinned chat slot
DEFAULT_OLLAMA_TIMEOUT_S = 600.0  # 10 minutes per turn — generous for slow hardware
# Ollama's default num_ctx is 2K-4K depending on version, which our 30-turn
# fixtures blow past on the first call (500 Internal Server Error). 16K is
# the sweet spot for our fixture set: large enough that fixture #11 (50-turn
# marathon) fits comfortably, small enough that KV cache stays around ~1.5 GB
# on Qwen3-30B-A3B (total RAM ~18-19 GB on 24 GB Apple Silicon — fits with
# headroom for OS).
DEFAULT_OLLAMA_NUM_CTX = 16384

DEFAULT_ANTHROPIC_MODEL = "claude-opus-4-7"
DEFAULT_ANTHROPIC_MAX_TOKENS = 1024


# ── Message-format translation ───────────────────────────────────────────────


def _strip_thinking(text: str) -> str:
    """Drop Qwen3 chain-of-thought prose from a response.

    Ollama 0.18 with ``think: false`` strips the opening ``<think>`` tag but
    leaves the model's chain-of-thought prose plus the closing ``</think>``
    in the response body. Scoring against that text produces false-positive
    guard hits because the chain-of-thought routinely names rejected
    candidates ("the user wants ``finalize_invoice``, not ``rollback_to_draft``")
    that match ``must_not_contain`` patterns even when the final answer is
    correct. We discard everything up to and including the last ``</think>``
    so the scorer sees only the model's final answer — same shape production
    would see if /no_think were honored.

    No-op for responses that don't contain ``</think>`` (Anthropic, models
    not in thinking mode).
    """
    if "</think>" not in text:
        return text
    return text.rsplit("</think>", 1)[-1].lstrip()


def _flatten_block_content(content: Any) -> str:
    """Render an Anthropic-style content list into a plain string.

    Used by Ollama, which (at the time of writing) accepts simple
    ``role/content`` messages plus a separate ``tool_calls`` /
    ``tool_call_id`` shape for tool turns. For non-tool blocks we just
    concatenate the text segments. Tool_use and tool_result blocks are
    handled by the per-adapter translator below — this helper is only for
    pure-text content lists.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                # tool_use / tool_result blocks are handled at the adapter level
        return "".join(parts)
    return str(content)


def _translate_messages_for_ollama(messages: list[dict]) -> list[dict]:
    """Translate Anthropic-style messages to Ollama's chat-API format.

    Ollama's ``/api/chat`` expects:
    - ``{"role": "user"|"assistant"|"system"|"tool", "content": "<str>"}``
    - Optional ``tool_calls`` on assistant messages with structured tool calls
    - ``tool`` messages reference the tool by name, not by id

    Tool_use → assistant message with ``tool_calls``.
    Tool_result → ``role: "tool"`` message with the result content as text.
    Plain text content (string OR list of text blocks) → flat string content.
    """
    out: list[dict] = []
    # Map our generated tool_use_id → tool name so we can fill in the
    # ``tool`` message's ``name`` field when we encounter the matching
    # tool_result block.
    pending_tool_names: dict[str, str] = {}

    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")

        if role == "assistant" and isinstance(content, list):
            tool_calls = []
            text_parts = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    tool_id = block.get("id", "")
                    tool_name = block.get("name", "")
                    pending_tool_names[tool_id] = tool_name
                    tool_calls.append({
                        "function": {
                            "name": tool_name,
                            "arguments": block.get("input", {}),
                        },
                    })
                elif block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
            ollama_msg: dict[str, Any] = {
                "role": "assistant",
                "content": "".join(text_parts),
            }
            if tool_calls:
                ollama_msg["tool_calls"] = tool_calls
            out.append(ollama_msg)
            continue

        if role == "user" and isinstance(content, list):
            # Could be a tool_result message OR a normal user message with text blocks
            tool_result_blocks = [
                b for b in content
                if isinstance(b, dict) and b.get("type") == "tool_result"
            ]
            if tool_result_blocks:
                for block in tool_result_blocks:
                    tool_use_id = block.get("tool_use_id", "")
                    out.append({
                        "role": "tool",
                        "name": pending_tool_names.get(tool_use_id, "unknown"),
                        "content": str(block.get("content", "")),
                    })
                continue
            # Plain user message with text blocks
            out.append({"role": "user", "content": _flatten_block_content(content)})
            continue

        # Default: scalar content
        out.append({"role": role, "content": _flatten_block_content(content)})

    return out


def _split_system_for_anthropic(messages: list[dict]) -> tuple[str, list[dict]]:
    """Anthropic's API takes ``system`` as a top-level argument, not as a
    message role. Pull any ``system`` messages out and concatenate them."""
    system_parts: list[str] = []
    rest: list[dict] = []
    for msg in messages:
        if msg.get("role") == "system":
            system_parts.append(_flatten_block_content(msg.get("content", "")))
        else:
            rest.append(msg)
    return "\n\n".join(p for p in system_parts if p), rest


# ── Ollama adapter ───────────────────────────────────────────────────────────


@dataclass
class OllamaChat:
    """ChatCallable that talks to a local Ollama instance.

    The eval default. Pins ``temperature: 0`` and a fixed ``seed``; if
    Ollama doesn't honor the seed for a particular model, the variance
    will show up in multi-seed eval runs and we'll catch it there.
    """

    model: str = DEFAULT_OLLAMA_MODEL
    base_url: str = DEFAULT_OLLAMA_BASE_URL
    seed: int = 42
    timeout_s: float = DEFAULT_OLLAMA_TIMEOUT_S
    num_ctx: Optional[int] = DEFAULT_OLLAMA_NUM_CTX  # None = let Ollama default
    # Qwen3 has /think and /no_think directives. By default models like
    # Qwen3-30B-A3B run with thinking ON, generating hundreds of tokens of
    # internal monologue before the actual answer. ADR 008 pins the v1
    # chat slot to /no_think — production runs without thinking for latency
    # reasons. The eval mirrors production: True by default. Set False if
    # you specifically want to A/B test thinking on/off.
    disable_thinking: bool = True

    @property
    def model_id(self) -> str:
        """Stable identifier embedded in baseline filenames and reports."""
        thinking_tag = "" if self.disable_thinking else "+think"
        return f"ollama:{self.model}@seed={self.seed}{thinking_tag}"

    async def __call__(self, messages: list[dict], system_prompt: str) -> str:
        translated = _translate_messages_for_ollama(messages)

        if system_prompt:
            translated = [{"role": "system", "content": system_prompt}] + translated

        options: dict[str, Any] = {"temperature": 0, "seed": self.seed}
        if self.num_ctx is not None:
            options["num_ctx"] = self.num_ctx

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": translated,
            "stream": False,
            "options": options,
        }
        # Qwen3 thinking-mode toggle. The /no_think directive in messages
        # does NOT disable thinking on Ollama 0.18 — the model still emits
        # the internal monologue and pays the latency cost. The working
        # mechanism is the top-level "think" boolean on the request body.
        # Empirically: think:false reduces a "say hi" turn from ~10s to
        # ~500ms on Qwen3-14B (no thinking-token decode). Default off
        # matches ADR 008's pinned chat-slot policy.
        if self.disable_thinking:
            payload["think"] = False

        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            response = await client.post(
                f"{self.base_url.rstrip('/')}/api/chat",
                json=payload,
            )
            if response.status_code >= 400:
                # Surface Ollama's error body so context-overflow / model-not-loaded
                # / unsupported-option errors are diagnosable from the traceback
                # instead of just "500 Internal Server Error".
                body = response.text[:1000]
                raise RuntimeError(
                    f"Ollama returned HTTP {response.status_code} for {self.model!r} "
                    f"(num_ctx={self.num_ctx}, history_len={len(translated)}): {body!r}"
                )
            data = response.json()

        message = data.get("message") or {}
        text = message.get("content")
        if not isinstance(text, str):
            raise RuntimeError(
                f"Ollama returned malformed response (no message.content): {data!r}"
            )
        # Even with `think: false`, Ollama 0.18 leaves the </think> close tag
        # plus the chain-of-thought prose in front of the real answer. Strip
        # it before returning so the scorer + downstream history see only the
        # final answer (matches what production-with-/no_think would emit).
        return _strip_thinking(text)


# ── Anthropic adapter ────────────────────────────────────────────────────────


@dataclass
class AnthropicChat:
    """ChatCallable that talks to Anthropic's hosted API.

    Opt-in only. Use for harness development loops or when local hardware
    can't host the chat-under-test model. Results from this adapter must
    NOT be promoted to the canonical baseline — they reflect a different
    model family than the production stack.

    The ``seed`` field is recorded in ``model_id`` for bookkeeping only —
    Anthropic's API does not accept a seed parameter. At temperature 0,
    the variance across "seeds" is the API's own non-determinism; it's
    typically small but not zero, and multi-seed runs against this
    adapter surface that variance honestly.
    """

    model: str = DEFAULT_ANTHROPIC_MODEL
    api_key: Optional[str] = None  # falls back to ANTHROPIC_API_KEY env var
    max_tokens: int = DEFAULT_ANTHROPIC_MAX_TOKENS
    seed: int = 0  # recorded only — not passed to API

    @property
    def model_id(self) -> str:
        return f"anthropic:{self.model}@seed={self.seed}"

    async def __call__(self, messages: list[dict], system_prompt: str) -> str:
        # Lazy-import so importing this module does not require the
        # anthropic SDK to be installed for callers who only use OllamaChat.
        from anthropic import AsyncAnthropic

        api_key = self.api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "AnthropicChat requires an api_key argument or the "
                "ANTHROPIC_API_KEY environment variable."
            )

        # Anthropic accepts our message format natively (it IS Anthropic
        # format). We only need to split out any system messages.
        sys_from_messages, body_messages = _split_system_for_anthropic(messages)
        full_system = "\n\n".join(p for p in (system_prompt, sys_from_messages) if p)

        client = AsyncAnthropic(api_key=api_key)
        try:
            response = await client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                temperature=0,
                system=full_system or "You are a helpful assistant.",
                messages=body_messages,
            )
        finally:
            await client.close()

        # Concatenate text blocks from the response
        parts: list[str] = []
        for block in response.content:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
        return "".join(parts)


# ── Factory ──────────────────────────────────────────────────────────────────


def make_chat(
    provider: str = "ollama",
    *,
    model: Optional[str] = None,
    seed: int = 42,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
) -> "OllamaChat | AnthropicChat":
    """Pick a chat adapter by provider name.

    Default is Ollama, per the project's posture: measure the production
    stack honestly. ``provider="anthropic"`` is the opt-in escape hatch
    for harness-development loops.
    """
    p = provider.strip().lower()
    if p == "ollama":
        return OllamaChat(
            model=model or DEFAULT_OLLAMA_MODEL,
            base_url=base_url or DEFAULT_OLLAMA_BASE_URL,
            seed=seed,
        )
    if p in ("anthropic", "claude"):
        return AnthropicChat(
            model=model or DEFAULT_ANTHROPIC_MODEL,
            api_key=api_key,
            seed=seed,
        )
    raise ValueError(
        f"Unknown chat provider {provider!r}. "
        f"Supported: 'ollama' (default), 'anthropic'."
    )


def make_chat_factory(
    provider: str = "ollama",
    *,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
):
    """Return a function ``(seed) -> ChatCallable``.

    Used by ``run_fixture_multi_seed`` to instantiate a fresh chat
    callable for each seed. For Ollama, the seed reaches the model via
    ``options.seed``; for Anthropic the seed is recorded in the model_id
    for bookkeeping but does not change model behavior (the API does not
    accept a seed parameter — variance at temperature 0 is small but not
    zero, and multi-seed runs surface it).
    """

    def factory(seed: int) -> "OllamaChat | AnthropicChat":
        return make_chat(
            provider,
            model=model,
            seed=seed,
            api_key=api_key,
            base_url=base_url,
        )

    return factory
