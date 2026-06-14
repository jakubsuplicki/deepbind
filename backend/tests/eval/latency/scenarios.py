"""Scenario definitions for the latency benchmark (ADR 011).

A scenario is a single (system_prompt, user_message, max_output_tokens) triple
plus a stable name. The harness runs each scenario × model × N timed runs and
records TTFT / decode-tps / total wall clock.

Two categories:

- **synthetic** — fixed-shape isolation scenarios (warm-short, prefill at
  approximate token sizes 1k / 4k / 16k, decode-throughput). These exist to
  isolate one phase of inference from the others.
- **fixture** — derived from the existing conversation eval fixtures so the
  numbers reflect realistic conversation shapes, not synthetic ones. v1 includes
  one fixture-derived scenario (long-conv-shallow); follow-on chunks add more.

This is a local-only build (ADR 015 removed all cloud LLM SDKs), so there is
no hosted-API "reference" comparison category — every scenario runs against
the local Ollama stack.

Token-size approximation: ~4 chars per English token is the standard ballpark.
Exact token counts vary by tokenizer, but the scenarios are about *shape*
(short / medium / long) and reproducibility — not about hitting an exact
token target. The padding text is deterministic (numbered lines) so the
same scenario produces byte-identical input across runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


CHARS_PER_TOKEN_APPROX = 4
"""English-text approximation. Polish / CJK / code is denser; the scenarios
intentionally use plain English so the size category is honest across runs."""


class ScenarioCategory(str, Enum):
    SYNTHETIC = "synthetic"
    FIXTURE = "fixture"


@dataclass(frozen=True)
class Scenario:
    """A single benchmark scenario.

    Frozen so a scenario object can be safely shared across runs without
    accidental mutation. Stable string keys for clean baseline JSON.
    """

    name: str
    category: ScenarioCategory
    system_prompt: str
    user_message: str
    max_output_tokens: int
    description: str

    def approx_input_tokens(self) -> int:
        """Rough token count of system + user. For reporting only — the harness
        prefers ``prompt_eval_count`` from the model's done event when available."""
        return (
            len(self.system_prompt) + len(self.user_message)
        ) // CHARS_PER_TOKEN_APPROX


# ── Helpers ──────────────────────────────────────────────────────────────────


_BASE_SYSTEM_PROMPT = (
    "You are a helpful assistant. Answer the user's question directly and "
    "concisely."
)


def _padding_text(target_chars: int) -> str:
    """Generate deterministic padding text of approximately ``target_chars``.

    Numbered lines like ``001: filler content for benchmark padding``. Avoids
    the lorem-ipsum tokenization artifacts (lots of two-letter words inflate
    token count vs char count) and stays compressible. The same target_chars
    always produces the same string.
    """
    line_template = "{idx:04d}: filler content for benchmark padding so the prompt reaches a known size."
    out: list[str] = []
    total = 0
    idx = 0
    while total < target_chars:
        line = line_template.format(idx=idx)
        out.append(line)
        total += len(line) + 1  # newline
        idx += 1
    return "\n".join(out)[:target_chars]


# ── v1 scenario set ─────────────────────────────────────────────────────────


def warm_short() -> Scenario:
    """Smallest possible turn — TTFT floor on a warm model."""
    return Scenario(
        name="warm-short",
        category=ScenarioCategory.SYNTHETIC,
        system_prompt=_BASE_SYSTEM_PROMPT,
        user_message="Say hi in one word.",
        max_output_tokens=8,
        description="Minimal turn against a warm model; measures TTFT floor.",
    )


def prefill_scenario(target_tokens: int) -> Scenario:
    """A prefill-stress scenario at the given approximate prompt size.

    System prompt is padded to ``target_tokens × CHARS_PER_TOKEN_APPROX`` chars;
    user message is short. The phase under test is prefill (encoding the
    system prompt) — output is bounded so decode doesn't dominate.
    """
    target_chars = target_tokens * CHARS_PER_TOKEN_APPROX
    padded_prompt = _BASE_SYSTEM_PROMPT + "\n\n" + _padding_text(target_chars)
    return Scenario(
        name=f"prefill-{target_tokens // 1000}k",
        category=ScenarioCategory.SYNTHETIC,
        system_prompt=padded_prompt,
        user_message="In one sentence, what's the capital of France?",
        max_output_tokens=32,
        description=(
            f"Prefill stress at ~{target_tokens // 1000}K input tokens. "
            f"User message and output are bounded; the phase under test "
            f"is prompt-eval throughput."
        ),
    )


def decode_throughput() -> Scenario:
    """Sustained decode benchmark — short prompt, large max_tokens."""
    return Scenario(
        name="decode-throughput",
        category=ScenarioCategory.SYNTHETIC,
        system_prompt=_BASE_SYSTEM_PROMPT,
        user_message=(
            "Write a 200-word neutral overview of the periodic table, "
            "covering its origin, structure, and main groups. Be specific."
        ),
        max_output_tokens=512,
        description="Short input, long output; measures sustained decode tokens/sec.",
    )


def chat_realistic() -> Scenario:
    """A realistic conversation shape derived from the existing eval fixtures.

    Mirrors fixture #1 (``long-conv-shallow``) at its assistant_target turn:
    medium-length system prompt + ~20-turn history compressed into a single
    user message that quotes the relevant prior turns. This is a realistic
    *shape* (not a fixture replay — the conversation harness already does
    that). Same scenario every run for determinism.
    """
    history_summary = "\n".join(
        f"User turn {i}: {('working on a CSS bug.' if i == 3 else 'casual chitchat about weekend plans.')}"
        for i in range(1, 25)
    )
    return Scenario(
        name="chat-realistic-shallow",
        category=ScenarioCategory.FIXTURE,
        system_prompt=(
            _BASE_SYSTEM_PROMPT
            + "\n\nThe user has been chatting with you for ~25 turns. "
            + "Earlier in the conversation they mentioned working on a "
            + "specific technical problem. Below is a summary of the prior turns:"
            + "\n\n" + history_summary
        ),
        user_message=(
            "Going back to what I mentioned earlier — what was that "
            "technical thing I said I was working on?"
        ),
        max_output_tokens=64,
        description=(
            "Realistic 25-turn-deep conversation shape; the model has to "
            "recall a topic-defining detail from turn 3."
        ),
    )


def default_scenarios() -> list[Scenario]:
    """The v1 default scenario set.

    Five scenarios cover the four phases that matter for perceived latency
    (TTFT-floor, prefill stress at 4K and 16K, sustained decode, realistic
    conversation shape), all run against the local Ollama stack.

    Cold-start is *not* included at v1 — it requires Ollama process control
    that's better handled in a follow-on chunk. Decode at 1K prefill is
    omitted as a duplicate of warm-short for our purposes.
    """
    return [
        warm_short(),
        prefill_scenario(4_000),
        prefill_scenario(16_000),
        decode_throughput(),
        chat_realistic(),
    ]
