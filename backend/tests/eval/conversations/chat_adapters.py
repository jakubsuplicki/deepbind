"""ChatCallable adapters for the conversation-replay runner (ADR 010).

One implementation of the ``ChatCallable`` Protocol:

- :class:`OllamaChat` — talks to a locally-running Ollama instance. The
  only adapter for the eval, since it measures the production stack honestly
  (the same models customers run, the same quantization, the same runtime).
  Slower per turn (~5–15 tok/s on 24 GB unified memory) but the numbers
  reflect what we actually ship. This is a local-only build (ADR 015 removed
  all cloud LLM SDKs); there is no hosted-API escape hatch.

Determinism: the adapter pins temperature to 0 and (where supported) a
fixed seed. Ollama honors ``options.seed`` on most models. Document the
caveat per result.

Message-format translation: messages with ``content`` as a list of blocks
(text / tool_use / tool_result) are the runner's canonical format. The
adapter translates to Ollama's native shape at the boundary. Tool_use /
tool_result blocks are honored because some launch fixtures (#2, #4, #7)
script tool interactions in the assistant turn history that precedes the
assistant_target turn.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

import httpx


# ── Defaults ─────────────────────────────────────────────────────────────────


DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_OLLAMA_MODEL = "qwen3:14b"  # v1 canonical chat model — see ADR 010 §"Issue 4"
# Was qwen3:30b-a3b until 2026-04-28; latency benchmark (ADR 011) discovered the
# 30B-A3B leaks chain-of-thought tokens despite think:false on Ollama 0.18.0,
# producing thinking prose instead of answers under tight num_predict caps and
# wasting decode budget under realistic caps. Qwen3-14B honors think:false
# correctly, fits comfortably in 24 GB unified memory (~9 GB weights vs ~17 GB),
# and benchmarked 8x faster on chat-realistic-shallow (1.8s p95 vs 14.6s p95).
# Per-machine canonical selection is ADR 012's chat-model self-test, which
# replaces this static default with a probe-driven pick at install time.
DEFAULT_OLLAMA_TIMEOUT_S = 600.0  # 10 minutes per turn — generous for slow hardware
# Ollama's default num_ctx is 2K-4K depending on version, which our 30-turn
# fixtures blow past on the first call (500 Internal Server Error). 16K is
# the sweet spot for our fixture set: large enough that fixture #11 (50-turn
# marathon) fits comfortably, small enough that KV cache stays around ~1.5 GB
# on Qwen3-30B-A3B (total RAM ~18-19 GB on 24 GB Apple Silicon — fits with
# headroom for OS).
DEFAULT_OLLAMA_NUM_CTX = 16384


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

    No-op for responses that don't contain ``</think>`` (models not in
    thinking mode).
    """
    if "</think>" not in text:
        return text
    return text.rsplit("</think>", 1)[-1].lstrip()


def _flatten_block_content(content: Any) -> str:
    """Render a block-list content value into a plain string.

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
    """Translate canonical block-style messages to Ollama's chat-API format.

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
    # internal monologue before the actual answer. v1 production runs without
    # thinking for latency reasons; the eval mirrors that with True by default.
    # Set False if you specifically want to A/B test thinking on/off.
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
        # matches v1 canonical chat-model policy.
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


# ── Factory ──────────────────────────────────────────────────────────────────


def make_chat(
    provider: str = "ollama",
    *,
    model: Optional[str] = None,
    seed: int = 42,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
) -> OllamaChat:
    """Pick a chat adapter by provider name.

    Only Ollama is supported: this is a local-only build (ADR 015 removed
    all cloud LLM SDKs), so the eval measures the production stack honestly
    and has no hosted-API escape hatch.
    """
    p = provider.strip().lower()
    if p == "ollama":
        return OllamaChat(
            model=model or DEFAULT_OLLAMA_MODEL,
            base_url=base_url or DEFAULT_OLLAMA_BASE_URL,
            seed=seed,
        )
    raise ValueError(
        f"Unknown chat provider {provider!r}. Supported: 'ollama' (only)."
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
    callable for each seed. The seed reaches the Ollama model via
    ``options.seed``.
    """

    def factory(seed: int) -> OllamaChat:
        return make_chat(
            provider,
            model=model,
            seed=seed,
            api_key=api_key,
            base_url=base_url,
        )

    return factory
