"""Conversation-replay runner (ADR 010).

Replays a hand-authored fixture under a given ContextStrategy and a
chat-model callable, scoring each ``assistant_target`` turn mechanically.
Pure pipeline — no websockets, no session-service plumbing — so the
strategy variable is isolated cleanly.

Scope (intentionally narrow at v1, per ADR 010 step 2):
- Replay scripted turns verbatim into a running history.
- At each ``assistant_target`` turn, hand the assembled context to a chat
  callable (injected; see ChatCallable Protocol) and capture the response.
- Score the response against ``expected_facts`` / ``must_not_contain``.
- Emit a stable-key result struct.

What this does NOT do at v1 (deferred to later steps in ADR 010):
- Retrieval / system-prompt augmentation. The fixtures' workspace_fixture
  precondition is recorded but not consulted; the runner sends the
  assembled history to the model with a fixed minimal system prompt.
  Documented gap; revisited when the gate-decision result indicates
  retrieval-aware variants are worth measuring.
- Live tool execution. ``assistant_target`` turns are not expected to
  invoke new tools; if the model emits a tool_call, the runner records
  it as an unexpected event and the turn fails. Tool mocks for scripted
  turns are honored at history-build time only.
- Judge protocol. Mechanical scoring only. Judge wraps this output later.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional, Protocol, runtime_checkable

from services.chat import ContextStrategy, FullHistoryStrategy

from .scorer import Severity, TurnScore, score_turn


# ── Public types ──────────────────────────────────────────────────────────────


@runtime_checkable
class ChatCallable(Protocol):
    """An awaitable that takes assembled context and returns response text.

    Pinning this as a Protocol lets the runner accept a stub for tests, an
    Anthropic adapter for hosted runs, an Ollama adapter for local runs,
    etc. without coupling to any single provider. Implementations must be
    deterministic given the same input — temperature 0, fixed seed.

    The ``messages`` argument is the strategy-assembled history (the same
    shape ``ClaudeService.stream_response`` accepts). ``system_prompt`` is
    a minimal fixed string at v1; we may extend later.
    """

    async def __call__(
        self, messages: list[dict], system_prompt: str
    ) -> str:
        ...


@dataclass
class TurnResult:
    """Outcome of one assistant_target turn under one seed."""

    turn_index: int
    seed: int
    response_text: str
    score: TurnScore
    latency_ms: float
    unexpected_tool_calls: list[str] = field(default_factory=list)


@dataclass
class FixtureResult:
    """Outcome of one fixture under one strategy.

    With multi-seed runs, ``turn_results`` contains ``len(seeds) ×
    target_turn_count`` entries. Aggregations are computed over the full
    set; the ``per_seed_*`` properties expose seed-level aggregates so
    bootstrap CIs (chunk 2) have something to resample.
    """

    fixture_id: str
    strategy_name: str
    chat_model_id: str  # opaque label; runner does not inspect
    target_turn_count: int
    seeds: list[int]
    turn_results: list[TurnResult]

    # ── Pass-rate aggregations ───────────────────────────────────────────────

    @property
    def mechanical_pass_rate(self) -> float:
        """Backwards-compatible alias for ``clean_pass_rate``."""
        return self.clean_pass_rate

    @property
    def clean_pass_rate(self) -> float:
        if not self.turn_results:
            return 0.0
        n = sum(1 for r in self.turn_results if r.score.severity is Severity.CLEAN_PASS)
        return n / len(self.turn_results)

    @property
    def confabulation_rate(self) -> float:
        if not self.turn_results:
            return 0.0
        n = sum(
            1 for r in self.turn_results if r.score.severity is Severity.CONFABULATION
        )
        return n / len(self.turn_results)

    @property
    def severity_distribution(self) -> dict[str, float]:
        """Fraction of turns in each severity bucket. Stable string keys
        (matches Severity enum values) for clean baseline-JSON diffs."""
        if not self.turn_results:
            return {s.value: 0.0 for s in Severity}
        total = len(self.turn_results)
        counts = Counter(r.score.severity.value for r in self.turn_results)
        return {s.value: counts.get(s.value, 0) / total for s in Severity}

    # ── Per-seed aggregations (variance + bootstrap inputs) ──────────────────

    def per_seed_clean_pass_rates(self) -> dict[int, float]:
        """Clean-pass rate computed per seed. Bootstrap CIs (chunk 2)
        sample over this distribution."""
        by_seed: dict[int, list[bool]] = {}
        for r in self.turn_results:
            by_seed.setdefault(r.seed, []).append(
                r.score.severity is Severity.CLEAN_PASS
            )
        return {
            seed: (sum(passes) / len(passes)) if passes else 0.0
            for seed, passes in by_seed.items()
        }

    @property
    def stdev_clean_pass_rate(self) -> float:
        """Standard deviation of per-seed clean-pass rate. ``0.0`` if
        only one seed (degenerate case — no variance estimate)."""
        rates = list(self.per_seed_clean_pass_rates().values())
        if len(rates) <= 1:
            return 0.0
        return statistics.stdev(rates)

    # ── Latency ──────────────────────────────────────────────────────────────

    @property
    def p50_latency_ms(self) -> float:
        return _percentile([r.latency_ms for r in self.turn_results], 0.5)

    @property
    def p95_latency_ms(self) -> float:
        return _percentile([r.latency_ms for r in self.turn_results], 0.95)


@dataclass
class MultiSeedFixtureResult(FixtureResult):
    """Marker subtype produced by ``run_fixture_multi_seed``.

    Identical fields to ``FixtureResult``; the type distinction lets
    callers assert "this result was produced from multiple seeds and
    therefore has meaningful variance." Equivalent to ``FixtureResult``
    when only one seed is supplied (variance is zero).
    """

    pass


# ── Helpers ───────────────────────────────────────────────────────────────────


_DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful assistant. Answer the user's question directly and "
    "concisely, using only information from the conversation history."
)


def _last_user_message_text(history: list[dict]) -> str:
    """Extract the most recent user message text from history.

    Skips user messages whose entire content is tool_result blocks (those
    are protocol-mandated tool responses, not real user inputs). Returns
    an empty string if no real user message is present yet — retrieval
    against an empty query returns no context, which is the right
    degenerate behavior for the first-turn case.
    """
    for msg in reversed(history):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            text_parts = [
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            text = "".join(text_parts)
            if text:
                return text
    return ""


async def _build_system_prompt_for_turn(
    history: list[dict],
    *,
    retrieval_enabled: bool,
    fallback_system_prompt: str,
    workspace_path: Optional[Path],
    graph_scope: Optional[str],
) -> str:
    """Resolve the system prompt for an assistant_target turn.

    When ``retrieval_enabled`` is ``False`` (the default), we hand back
    the fixed fallback prompt — fast, deterministic, and isolates the
    strategy variable. When ``True``, we mirror production by calling
    ``build_system_prompt_with_stats`` against the most recent user
    message; this exercises retrieval-augmented context the way the
    shipped chat path does.

    The ``services.claude`` import is local so that running the eval
    without retrieval doesn't pay the cost of importing the retrieval
    pipeline (which transitively pulls in embeddings, the SQLite index,
    and a fair amount of other infrastructure).
    """
    if not retrieval_enabled:
        return fallback_system_prompt

    user_message = _last_user_message_text(history)
    from services.claude import build_system_prompt_with_stats

    prompt, _stats = await build_system_prompt_with_stats(
        user_message,
        workspace_path=workspace_path,
        graph_scope=graph_scope,
    )
    return prompt


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((len(s) - 1) * pct))))
    return s[k]


def load_fixture(path: Path) -> dict:
    """Load and return the fixture JSON. Schema validation is intentionally
    lightweight — fixture-author bugs should fail loudly during a run."""
    with path.open("r", encoding="utf-8") as f:
        fixture = json.load(f)
    if "turns" not in fixture or not isinstance(fixture["turns"], list):
        raise ValueError(f"fixture {path} missing or malformed 'turns'")
    return fixture


def _build_scripted_assistant_message(turn: dict) -> dict:
    """Convert an ``assistant_scripted`` turn into a chat-format message.

    Two shapes:
    - Plain text: ``content`` is a string; emit ``{"role": "assistant",
      "content": "..."}``.
    - Tool call: ``expected_tool_calls`` present; emit a structured
      assistant message with a ``tool_use`` block, mirroring the format
      ``ClaudeService`` and ``LLMService`` consume.

    The ``[tool_call:...]`` text in the fixture is informational for
    humans; the structured ``expected_tool_calls`` field drives the
    actual message construction.
    """
    expected_calls = turn.get("expected_tool_calls") or []
    if expected_calls:
        # Use the first call; current fixtures emit one tool call per
        # scripted assistant turn. If multi-call scripted turns are added
        # later, extend here.
        call = expected_calls[0]
        return {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": f"scripted_{call.get('name', 'tool')}_{id(turn)}",
                    "name": call.get("name", ""),
                    "input": call.get("args", {}),
                }
            ],
        }
    return {"role": "assistant", "content": turn.get("content", "")}


def _build_tool_result_message(turn: dict, tool_use_id: str) -> dict:
    """Convert a ``tool_result`` fixture turn into a tool_result message
    block addressed to the matching ``tool_use_id``."""
    return {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": turn.get("content", ""),
            }
        ],
    }


# ── Replay loop ───────────────────────────────────────────────────────────────


async def run_fixture(
    fixture: dict,
    *,
    strategy: ContextStrategy,
    chat: ChatCallable,
    chat_model_id: str = "unknown",
    system_prompt: Optional[str] = None,
    seed: int = 0,
    retrieval_enabled: bool = False,
    workspace_path: Optional[Path] = None,
    graph_scope: Optional[str] = None,
) -> FixtureResult:
    """Replay one fixture under one strategy at one seed, return the scored result.

    Pure async — caller drives the event loop and supplies the chat
    callable. Determinism is the caller's responsibility (pin temperature
    in the chat adapter); the runner adds no randomness of its own. The
    ``seed`` argument is passed through into each ``TurnResult`` for
    bookkeeping; the chat callable is responsible for actually applying
    it (e.g., ``OllamaChat`` accepts ``seed`` in its constructor).

    When ``retrieval_enabled=True``, the runner mirrors production by
    augmenting the system prompt at each ``assistant_target`` turn with
    retrieved notes (via ``build_system_prompt_with_stats``). This
    exercises the same retrieval path the shipped chat uses, which is
    important because the deployed compaction strategy operates against
    a retrieval-augmented system prompt — not the bare history. Pin the
    ``workspace_path`` to a reference fixture for reproducibility.
    """
    sys_prompt = system_prompt if system_prompt is not None else _DEFAULT_SYSTEM_PROMPT
    history: list[dict] = []
    last_tool_use_id: Optional[str] = None
    target_turn_count = 0
    turn_results: list[TurnResult] = []

    for turn_index, turn in enumerate(fixture["turns"]):
        role = turn.get("role")

        if role == "user":
            history.append({"role": "user", "content": turn.get("content", "")})

        elif role == "assistant_scripted":
            msg = _build_scripted_assistant_message(turn)
            history.append(msg)
            # Capture the tool_use_id so the next tool_result can reference it
            if isinstance(msg["content"], list):
                for block in msg["content"]:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        last_tool_use_id = block["id"]
                        break

        elif role == "tool_result":
            if last_tool_use_id is None:
                raise ValueError(
                    f"fixture {fixture['id']} turn {turn_index}: tool_result "
                    "without preceding scripted tool_use"
                )
            history.append(_build_tool_result_message(turn, last_tool_use_id))
            last_tool_use_id = None

        elif role == "assistant_target":
            target_turn_count += 1
            assembled = strategy.assemble(history)
            if not isinstance(assembled, list):
                raise TypeError(
                    f"strategy {strategy.name!r} returned "
                    f"{type(assembled).__name__}; expected list"
                )

            turn_system_prompt = await _build_system_prompt_for_turn(
                history,
                retrieval_enabled=retrieval_enabled,
                fallback_system_prompt=sys_prompt,
                workspace_path=workspace_path,
                graph_scope=graph_scope,
            )

            t0 = time.perf_counter()
            response = await chat(assembled, turn_system_prompt)
            latency_ms = (time.perf_counter() - t0) * 1000.0

            if not isinstance(response, str):
                raise TypeError(
                    f"chat callable returned {type(response).__name__}; expected str"
                )

            score = score_turn(turn, response)
            turn_results.append(
                TurnResult(
                    turn_index=turn_index,
                    seed=seed,
                    response_text=response,
                    score=score,
                    latency_ms=latency_ms,
                )
            )
            # Append the model's response so subsequent turns see it
            history.append({"role": "assistant", "content": response})

        else:
            raise ValueError(
                f"fixture {fixture['id']} turn {turn_index}: unknown role {role!r}"
            )

    return FixtureResult(
        fixture_id=fixture["id"],
        strategy_name=strategy.name,
        chat_model_id=chat_model_id,
        target_turn_count=target_turn_count,
        seeds=[seed],
        turn_results=turn_results,
    )


# ── Multi-seed wrapper ───────────────────────────────────────────────────────


ChatFactory = Callable[[int], ChatCallable]
"""A factory that produces a ``ChatCallable`` configured for a given seed.

Used by ``run_fixture_multi_seed`` to instantiate one chat callable per
seed. The factory is the seam through which the seed actually reaches
the model — without it, multi-seed would just label identical runs with
different seed numbers.
"""


async def run_fixture_multi_seed(
    fixture: dict,
    *,
    strategy: ContextStrategy,
    chat_factory: ChatFactory,
    seeds: list[int],
    chat_model_id: str = "unknown",
    system_prompt: Optional[str] = None,
    retrieval_enabled: bool = False,
    workspace_path: Optional[Path] = None,
    graph_scope: Optional[str] = None,
) -> MultiSeedFixtureResult:
    """Replay one fixture under one strategy across multiple seeds.

    Returns a ``MultiSeedFixtureResult`` whose ``turn_results`` is the
    concatenation of every seed's turns; aggregations (clean_pass_rate,
    severity_distribution, stdev_clean_pass_rate) are computed across
    all of them. Per-seed breakdowns are available via
    ``per_seed_clean_pass_rates``.

    The seeds are run sequentially. Parallel execution would be sound
    (each seed produces an independent chat callable) but is left as a
    future optimization — keeping it sequential makes failure modes
    easier to reason about for the gate decision.
    """
    if not seeds:
        raise ValueError("seeds must be a non-empty list")
    if len(seeds) != len(set(seeds)):
        raise ValueError(f"seeds must be unique, got {seeds}")

    all_turn_results: list[TurnResult] = []
    target_turn_count = 0
    for seed in seeds:
        chat = chat_factory(seed)
        result = await run_fixture(
            fixture,
            strategy=strategy,
            chat=chat,
            chat_model_id=chat_model_id,
            system_prompt=system_prompt,
            seed=seed,
            retrieval_enabled=retrieval_enabled,
            workspace_path=workspace_path,
            graph_scope=graph_scope,
        )
        all_turn_results.extend(result.turn_results)
        # target_turn_count is the same across seeds (same fixture)
        target_turn_count = result.target_turn_count

    return MultiSeedFixtureResult(
        fixture_id=fixture["id"],
        strategy_name=strategy.name,
        chat_model_id=chat_model_id,
        target_turn_count=target_turn_count,
        seeds=list(seeds),
        turn_results=all_turn_results,
    )


# ── Convenience for tests / CLI ──────────────────────────────────────────────


def run_fixture_sync(
    fixture: dict,
    *,
    strategy: Optional[ContextStrategy] = None,
    chat: ChatCallable,
    chat_model_id: str = "unknown",
    system_prompt: Optional[str] = None,
    seed: int = 0,
    retrieval_enabled: bool = False,
    workspace_path: Optional[Path] = None,
    graph_scope: Optional[str] = None,
) -> FixtureResult:
    """Sync wrapper for the async replay loop — useful in pytest."""
    return asyncio.run(
        run_fixture(
            fixture,
            strategy=strategy or FullHistoryStrategy(),
            chat=chat,
            chat_model_id=chat_model_id,
            system_prompt=system_prompt,
            seed=seed,
            retrieval_enabled=retrieval_enabled,
            workspace_path=workspace_path,
            graph_scope=graph_scope,
        )
    )


def run_fixture_multi_seed_sync(
    fixture: dict,
    *,
    strategy: Optional[ContextStrategy] = None,
    chat_factory: ChatFactory,
    seeds: list[int],
    chat_model_id: str = "unknown",
    system_prompt: Optional[str] = None,
    retrieval_enabled: bool = False,
    workspace_path: Optional[Path] = None,
    graph_scope: Optional[str] = None,
) -> MultiSeedFixtureResult:
    """Sync wrapper for the multi-seed replay loop."""
    return asyncio.run(
        run_fixture_multi_seed(
            fixture,
            strategy=strategy or FullHistoryStrategy(),
            chat_factory=chat_factory,
            seeds=seeds,
            chat_model_id=chat_model_id,
            system_prompt=system_prompt,
            retrieval_enabled=retrieval_enabled,
            workspace_path=workspace_path,
            graph_scope=graph_scope,
        )
    )
