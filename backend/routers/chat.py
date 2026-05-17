import asyncio
import hashlib
import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect

from services import session_service
from services import specialist_service
from services.entitlement_gate import require_functional
from services.chat import DEFAULT_STRATEGY, ContextStrategy
from services.system_prompt import (
    StreamEvent,
    attach_retrieval_to_user_message,
    build_system_prompt,
    build_system_prompt_with_stats,
)
from services.ollama_dispatcher import OllamaDispatchConfig, OllamaDispatcher
from services.tools import TOOLS, ToolNotFoundError, execute_tool
from services.token_tracking import log_usage
from services.workspace_service import get_api_key
from services.ws_send_queue import attach as _attach_send_queue, detach as _detach_send_queue, queue_for as _queue_for

logger = logging.getLogger(__name__)

# ── Per-turn prefill-cost diagnostic (TEMPORARY) ────────────────────────────
#
# Investigation of "every-turn 30s+ TTFT" reported during G4b6 cold-launch
# smoke. Hypothesis: the system prompt embeds retrieval output, so the prefix
# changes turn-to-turn → Ollama's KV cache prefix-match fails at token 0 →
# full re-prefill (~3-5K tokens) every turn. This logger emits one structured
# line per chat turn so we can confirm or reject that hypothesis from the
# sidecar log without scraping the WS payload.
#
# Track per-session: turn counter, last system-prompt SHA so we can flag when
# the prefix actually mutated. Cap memory at 256 sessions (FIFO) so an
# always-on instance doesn't grow unbounded.
_PERF_TURN_STATE: "dict[str, tuple[int, str]]" = {}
_PERF_TURN_STATE_CAP = 256


def _prefill_log(
    *,
    session_id: str,
    system_prompt: str,
    prompt_stats: dict,
    metrics_acc: list[int],
    tool_acc: list[int],
    model: Optional[str],
) -> None:
    sp_hash = hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()[:12]
    retrieval_block = prompt_stats.get("retrieval_block", "") or ""
    rb_hash = (
        hashlib.sha256(retrieval_block.encode("utf-8")).hexdigest()[:12]
        if retrieval_block else "-"
    )
    prev = _PERF_TURN_STATE.get(session_id)
    turn_no = (prev[0] + 1) if prev else 1
    prefix_stable = bool(prev) and prev[1] == sp_hash
    if len(_PERF_TURN_STATE) >= _PERF_TURN_STATE_CAP and session_id not in _PERF_TURN_STATE:
        _PERF_TURN_STATE.pop(next(iter(_PERF_TURN_STATE)), None)
    _PERF_TURN_STATE[session_id] = (turn_no, sp_hash)

    payload = _build_metrics_payload(metrics_acc) or {}
    logger.info(
        "chat_turn session=%s turn=%d sp_hash=%s prefix_stable=%s rb_hash=%s "
        "sp_total_tok=%s sp_ctx_tok=%s ctx_truncated=%s "
        "prefill_count=%s prefill_ms=%s ttft_ms=%s load_ms=%s "
        "eval_count=%s total_ms=%s decode_tps=%s prefill_tps=%s "
        "tool_calls=%s tool_rounds=%s model=%s",
        session_id, turn_no, sp_hash, prefix_stable, rb_hash,
        prompt_stats.get("total_tokens"),
        prompt_stats.get("context_tokens"),
        prompt_stats.get("context_truncated"),
        payload.get("prompt_eval_count"),
        round(metrics_acc[1] / 1_000_000, 1) if metrics_acc[5] else None,
        payload.get("ttft_ms"),
        payload.get("load_ms"),
        # eval_count and total_ms catch the hidden-thinking case: a turn
        # where ttft is fast and decode_tps is normal, but total_ms is
        # large and eval_count is much bigger than the visible reply
        # length, means the model emitted a chain-of-thought block we
        # stripped from display. Diagnoses Qwen3-class models leaking
        # thinking despite think:False — happens occasionally on specific
        # prompts even with the parameter set.
        payload.get("eval_count"),
        payload.get("total_ms"),
        payload.get("decode_tps"),
        payload.get("prefill_tps"),
        tool_acc[0] if tool_acc else 0,
        tool_acc[1] if tool_acc else 0,
        model,
    )

router = APIRouter(prefix="/api/chat", tags=["chat"])


async def _send_event(ws: WebSocket, event_type: str, **fields) -> None:
    """Send an event to the WebSocket via the per-WS send queue.

    Routing all WS writes through `services.ws_send_queue.WSSendQueue`
    keeps the per-session dispatch lock decoupled from WS write latency.
    Without it, a sleeping WKWebView (App Nap between turns on macOS)
    can block `ws.send_json` for 20-30s on the trailing `done` event,
    holding the lock and stalling the user's next message — see
    docs/features/chat.md "WS send queue" subsection.

    Tests/dev paths that bypass `attach_send_queue()` (e.g. raw
    WebSocket fixtures) fall back to a direct send so the contract is
    a strict superset of the old behavior.
    """
    bus = _queue_for(ws)
    payload = {"type": event_type, **fields}
    if bus is None:
        await ws.send_json(payload)
        return
    await bus.enqueue(payload)


# ── Per-turn telemetry accumulator (ADR 005 §C trigger 2) ───────────────────
#
# `metrics_acc` is a 6-slot list threaded through `_handle_message` and
# `_stream_follow_up`. Per-round usage events update it in place; the final
# `done` emission rolls it up into the `metrics` payload the frontend
# attaches to the just-finished assistant message.
#
# Slot layout — keep in lockstep with the helpers below:
#   [0] sum eval_duration_ns          (decode pass, all rounds)
#   [1] sum prompt_eval_duration_ns   (prefill pass, all rounds)
#   [2] first round's load_duration_ns + prompt_eval_duration_ns (TTFT)
#   [3] first round's load_duration_ns
#   [4] sum total_duration_ns         (wall clock, all rounds)
#   [5] flag — has any round reported timings? 0/1
#   [6] sum eval_count                (decode tokens, all rounds)
#   [7] sum prompt_eval_count         (prefill tokens, all rounds)
#
# Why a list of slots instead of a TypedDict: matches the existing
# `usage_acc` / `tool_acc` callsite shape and stays cheap to thread
# through the recursive `_stream_follow_up` without a class.
#
# Why we track token counts here too rather than reading from
# `usage_acc`: `usage_acc` is the billing accumulator and intentionally
# is *not* reset on the OOM-retry path (failed round's tokens still
# flow into the per-turn token log — pre-existing accounting behavior).
# The metrics surface needs to reset on OOM-retry so decode_tps and
# prefill_tps are computed against *only* the round whose model
# `done.model` reports. Decoupling here keeps the two concerns clean.

_M_EVAL_NS = 0
_M_PREFILL_NS = 1
_M_TTFT_NS = 2
_M_LOAD_NS = 3
_M_TOTAL_NS = 4
_M_HAS_TIMINGS = 5
_M_EVAL_COUNT = 6
_M_PREFILL_COUNT = 7


def _new_metrics_acc() -> list[int]:
    return [0, 0, 0, 0, 0, 0, 0, 0]


def _update_metrics_acc(acc: list[int], event: StreamEvent) -> None:
    """Fold one round's `usage` event into the per-turn accumulator.

    "Timed round" requires at least an `eval_duration_ns` or a
    `prompt_eval_duration_ns`. A `total_duration_ns` alone (e.g. an
    in-flight cancel where Ollama reports wall-clock only) is not
    sufficient — without a decode or prefill measurement we can't
    compute either of the two ratios the watcher cares about, and
    marking the round as timed would leak a `ttft_ms: 0` into the
    payload that the frontend would render as a real "0 ms" reading.

    On rounds with no timings at all, this helper is a no-op; the
    caller's `usage_acc` still tracks the token counts for billing.
    """
    if event.eval_duration_ns is None and event.prompt_eval_duration_ns is None:
        return
    if not acc[_M_HAS_TIMINGS]:
        # First round with timings — capture the user's *felt* TTFT as
        # the load + prefill of this round. Subsequent rounds accrue to
        # the wall-clock + decode totals but don't move TTFT.
        first_load = event.load_duration_ns or 0
        first_prefill = event.prompt_eval_duration_ns or 0
        acc[_M_TTFT_NS] = first_load + first_prefill
        acc[_M_LOAD_NS] = first_load
        acc[_M_HAS_TIMINGS] = 1
    acc[_M_EVAL_NS] += event.eval_duration_ns or 0
    acc[_M_PREFILL_NS] += event.prompt_eval_duration_ns or 0
    acc[_M_TOTAL_NS] += event.total_duration_ns or 0
    acc[_M_EVAL_COUNT] += event.output_tokens or 0
    acc[_M_PREFILL_COUNT] += event.input_tokens or 0


def _build_metrics_payload(acc: list[int]) -> Optional[dict]:
    """Render the accumulator into the `metrics` field of the `done` event.

    Returns None when no round reported timings — the frontend treats an
    absent `metrics` field as "no telemetry this turn" and suppresses
    the per-turn line + skips the health-watcher sample.

    All inputs come from `acc` — including token counts — so an
    OOM-retry that resets the accumulator produces a payload describing
    *only* the retry round, even though `usage_acc` still carries the
    failed round's tokens for billing.
    """
    if not acc[_M_HAS_TIMINGS]:
        return None
    eval_count = acc[_M_EVAL_COUNT]
    prompt_eval_count = acc[_M_PREFILL_COUNT]
    eval_ns = acc[_M_EVAL_NS]
    prefill_ns = acc[_M_PREFILL_NS]
    decode_tps = (eval_count / (eval_ns / 1_000_000_000)) if (eval_count and eval_ns) else None
    prefill_tps = (prompt_eval_count / (prefill_ns / 1_000_000_000)) if (prompt_eval_count and prefill_ns) else None
    payload: dict = {
        "eval_count": eval_count,
        "prompt_eval_count": prompt_eval_count,
        "ttft_ms": round(acc[_M_TTFT_NS] / 1_000_000, 1),
        "load_ms": round(acc[_M_LOAD_NS] / 1_000_000, 1),
        "total_ms": round(acc[_M_TOTAL_NS] / 1_000_000, 1),
    }
    if decode_tps is not None:
        payload["decode_tps"] = round(decode_tps, 2)
    if prefill_tps is not None:
        payload["prefill_tps"] = round(prefill_tps, 2)
    return payload


async def _run_tool(event: StreamEvent, session_id: str = "", api_key: str = "", specialist_id: str = "") -> str:
    try:
        return await execute_tool(
            event.name,
            event.tool_input or {},
            session_id=session_id,
            api_key=api_key or None,
            specialist_id=specialist_id or None,
        )
    except ToolNotFoundError:
        return f"Unknown tool: {event.name}"
    except Exception as exc:
        return f"Tool error: {exc}"


_MEMORY_MUTATING_TOOLS = frozenset({"write_note", "append_note", "create_plan", "update_plan"})

# Debounce background session saves: track pending save tasks per session
# so we cancel the previous scheduled save when a new reply arrives.
_pending_saves: dict[str, asyncio.Task] = {}


async def _save_session_bg(session_id: str) -> None:
    """Background task: save session to memory note + update graph.

    Debounced: waits 2 seconds so rapid-fire replies don't cause
    concurrent SQLite writes. Only the last scheduled save actually runs.
    """
    try:
        await asyncio.sleep(2)
        await session_service.save_session_to_memory(session_id)
    except asyncio.CancelledError:
        pass  # Expected when a newer save supersedes this one
    except Exception:
        logger.warning("Background save_session_to_memory failed for %s", session_id)
    finally:
        _pending_saves.pop(session_id, None)


def _schedule_session_save(session_id: str) -> None:
    """Schedule a debounced background save, cancelling any pending one."""
    old_task = _pending_saves.pop(session_id, None)
    if old_task and not old_task.done():
        old_task.cancel()
    _pending_saves[session_id] = asyncio.ensure_future(_save_session_bg(session_id))


async def _emit_memory_changed(ws: WebSocket, tool_name: str, tool_input: dict) -> None:
    """Emit memory_changed event for tools that modify notes."""
    if tool_name in _MEMORY_MUTATING_TOOLS:
        path = tool_input.get("path", "")
        await _send_event(ws, "memory_changed", path=path, action=tool_name)


# Soft cap for a single tool_result sent to the model. Larger payloads are
# truncated with head+tail preserved so the model can still reason about
# structure without paying for the full blob in every subsequent tool round.
_TOOL_RESULT_SOFT_CAP = 8000  # chars (~2000 tokens)
# When tool rounds cascade (model calls another tool after seeing a result),
# old tool_results in prior rounds are compacted more aggressively — the model
# already "read" them, so we only keep a short reminder of the payload.
_STALE_TOOL_RESULT_CAP = 600  # chars (~150 tokens)


def _truncate_tool_result(text: str, cap: int) -> str:
    """Truncate a tool_result keeping head + tail so structure is visible.

    The marker tells the model the content is abbreviated, which preserves
    reasoning quality for inspection-style questions ("did this field exist?").
    """
    if len(text) <= cap:
        return text
    head_len = int(cap * 0.7)
    tail_len = cap - head_len - 80  # reserve space for marker
    if tail_len < 40:
        return text[:cap] + "\n... [truncated]"
    return (
        text[:head_len]
        + f"\n\n... [truncated {len(text) - head_len - tail_len} chars for token budget] ...\n\n"
        + text[-tail_len:]
    )


def _compact_stale_tool_results(messages: list[dict]) -> list[dict]:
    """Compact older tool_results when we're about to send another tool round.

    The *last* tool_result stays full (the model just received it). Earlier
    tool_results get collapsed to ``_STALE_TOOL_RESULT_CAP`` chars — the model
    has already used them, so a reminder is enough to keep citations intact.
    """
    # Find indices of tool_result blocks in user messages
    tool_result_indices = []
    for i, msg in enumerate(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tool_result_indices.append(i)
                break

    if len(tool_result_indices) <= 1:
        return messages  # nothing stale to compact

    # Keep last one intact; compact everything earlier
    stale_indices = set(tool_result_indices[:-1])
    compacted: list[dict] = []
    for i, msg in enumerate(messages):
        if i not in stale_indices:
            compacted.append(msg)
            continue
        new_content = []
        for block in msg.get("content", []):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                original = block.get("content", "")
                if isinstance(original, str) and len(original) > _STALE_TOOL_RESULT_CAP:
                    new_content.append({
                        **block,
                        "content": _truncate_tool_result(original, _STALE_TOOL_RESULT_CAP),
                    })
                else:
                    new_content.append(block)
            else:
                new_content.append(block)
        compacted.append({**msg, "content": new_content})
    return compacted


def _build_tool_messages(
    messages: list[dict],
    event: StreamEvent,
    result: str,
) -> list[dict]:
    # Cap oversized tool results so a single huge read_note / search doesn't
    # dominate the context budget of every subsequent tool round.
    capped_result = _truncate_tool_result(result, _TOOL_RESULT_SOFT_CAP)
    return messages + [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": event.tool_use_id,
                    "name": event.name,
                    "input": event.tool_input or {},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": event.tool_use_id,
                    "content": capped_result,
                }
            ],
        },
    ]


MAX_TOOL_ROUNDS = 5


async def _stream_follow_up(
    ws: WebSocket,
    claude,  # OllamaDispatcher (legacy parameter name kept for callers)
    tool_messages: list[dict],
    system_prompt: str,
    tools: list[dict],
    session_id: str,
    api_key: str,
    usage_acc: list[int],
    depth: int = 1,
    tool_acc: list[int] | None = None,
    specialist_id: str = "",
    metrics_acc: list[int] | None = None,
) -> str:
    """Stream the follow-up turn after tool execution.

    Supports recursive tool calls up to MAX_TOOL_ROUNDS total.
    Returns accumulated text.

    Scope boundary (ADR 010): the ContextStrategy from ``_handle_message``
    is **not** re-applied here. Tool-loop messages are built incrementally
    by ``_build_tool_messages`` from the already-assembled initial context,
    and the in-loop ``_compact_stale_tool_results`` handles the only
    legitimate compaction that should happen mid-tool-loop (collapsing
    older tool_result payloads). A future strategy that wants to compact
    *during* a tool-loop must extend this contract explicitly — silent
    re-assembly here would break tool_use / tool_result pairing and
    corrupt the eval baseline.
    """
    text = ""
    pending_tools: list[StreamEvent] = []

    async for event in claude.stream_response(
        messages=tool_messages,
        system_prompt=system_prompt,
        tools=tools,
    ):
        if event.type == "text_delta":
            text += event.content
            await _send_event(ws, "text_delta", content=event.content)
        elif event.type == "tool_use":
            pending_tools.append(event)
        elif event.type == "usage":
            usage_acc[0] += event.input_tokens
            usage_acc[1] += event.output_tokens
            if metrics_acc is not None:
                _update_metrics_acc(metrics_acc, event)
        elif event.type == "error":
            await _send_event(ws, "error", content=event.content)

    # If Claude wants tool calls, execute them and recurse
    if pending_tools and depth < MAX_TOOL_ROUNDS:
        if tool_acc is not None:
            tool_acc[0] += len(pending_tools)
            tool_acc[1] += 1
        next_messages = tool_messages
        for tool_event in pending_tools:
            await _send_event(ws, "tool_use", name=tool_event.name, input=tool_event.tool_input)
            session_service.record_tool_use(session_id, tool_event.name)
            result = await _run_tool(tool_event, session_id=session_id, api_key=api_key, specialist_id=specialist_id)
            await _send_event(ws, "tool_result", name=tool_event.name, content=result)
            await _emit_memory_changed(ws, tool_event.name, tool_event.tool_input or {})
            next_messages = _build_tool_messages(next_messages, tool_event, result)

        # Compact older tool_results before another round so the model doesn't
        # re-pay for payloads it has already read. The most recent result stays
        # intact; earlier ones are collapsed to a short reminder.
        next_messages = _compact_stale_tool_results(next_messages)

        text += await _stream_follow_up(
            ws, claude, next_messages, system_prompt, tools,
            session_id, api_key, usage_acc, depth + 1, tool_acc=tool_acc,
            specialist_id=specialist_id, metrics_acc=metrics_acc,
        )

    return text


async def _apply_memory_pressure_swap(
    *,
    ws: WebSocket,
    provider: Optional[str],
    model: Optional[str],
    base_url: Optional[str],
    ctx_len_tokens: Optional[int],
) -> tuple[Optional[str], bool]:
    """Pre-dispatch model swap.

    Now scoped to the explicit lightweight-mode toggle (ADR 005 §C trigger 3).
    The auto-downgrade pre-flight (ADR 005 §C trigger 2) was removed: it
    gated dispatch on `psutil.virtual_memory().available × 0.8 >= footprint`,
    which on macOS unified memory ignores the reclaimable inactive/cached
    pool that Ollama itself can use. The check produced its own failure mode
    (floor-refused → "Insufficient RAM" toast) on machines where Ollama
    would have happily mmap-loaded the model — confirmed empirically on a
    24 GB Apple Silicon box where a 30B model loaded fine from the terminal
    but the in-app 8B was being refused. The OOM-retry-walk-the-ladder
    loop downstream (trigger 1) is the real safety net: if the load
    actually OOMs we step down. No reason to pre-emptively second-guess.

    Returns ``(new_model_litellm, floor_refused)``. ``floor_refused`` is
    now always False; kept for caller signature stability.

    No-op for Ollama models that aren't in the catalog (custom user pulls
    or off-ladder dev tags).
    """
    if not model:
        return model, False

    from services import memory_pressure_monitor as mpm
    from services.ollama_service import DEFAULT_OLLAMA_BASE_URL, list_installed_models

    requested_entry = mpm.find_entry_by_ollama_model(model)
    if requested_entry is None:
        return model, False

    if not _is_lightweight_mode_on():
        return model, False

    try:
        installed_raw = await list_installed_models(base_url or DEFAULT_OLLAMA_BASE_URL)
    except Exception:  # noqa: BLE001 — network/runtime — skip the swap, not the turn
        logger.warning("memory_pressure: list_installed_models failed; skipping lightweight pin")
        return model, False
    installed_tags = [m.get("name", "") for m in installed_raw if m.get("name")]

    try:
        tier = mpm.current_tier()
    except Exception:  # noqa: BLE001 — psutil/sysctl env-dependent
        logger.warning("memory_pressure: tier probe failed; skipping lightweight pin")
        return model, False

    # Lightweight mode hard-pins the active model to the smallest installed
    # rung on the tier's ladder, regardless of current free RAM. User
    # explicitly opted in to this behaviour.
    floor = mpm.floor_entry_for_tier(tier, installed_ollama_tags=installed_tags)
    if floor is not None and floor.id != requested_entry.id:
        await _send_event(
            ws,
            "warning",
            content=f"Lightweight mode — using {floor.label or floor.id}",
        )
        return floor.ollama_model, False
    return model, False


def _is_lightweight_mode_on() -> bool:
    """Read the lightweight-mode flag from the workspace preferences.

    Lazy import to dodge the test-fixture chicken-and-egg: ``get_settings``
    is monkey-patched per test, so we resolve through it on every call
    rather than caching at module import.
    """
    try:
        from config import get_settings
        from services import preference_service
        prefs = preference_service.load_preferences(
            workspace_path=get_settings().workspace_path,
        )
        return prefs.get("lightweight_mode", "false") == "true"
    except Exception:  # noqa: BLE001 — env-dependent; never fail a turn over this
        return False


async def _ladder_step_after_oom(
    *,
    provider: Optional[str],
    model: Optional[str],
    base_url: Optional[str],
    ctx_len_tokens: Optional[int],
) -> tuple[Optional[str], Optional[str]]:
    """Pick the next ladder step after an OOM (§C trigger 1).

    Returns ``(new_model_litellm, warning_text)`` on success, or
    ``(None, None)`` when no smaller installed model fits — caller surfaces
    a user-facing error in that case.

    Different from the pre-flight swap in two ways: (1) only Ollama OOM
    paths reach here, so the provider check is implicit; (2) the picker is
    seeded with the *failed* model — by re-checking against current free
    RAM (which can have dropped further since the dispatch attempt) the
    same model that just OOMed will be filtered as `over_footprint` and
    skipped. If the picker still returns the same model, treat it as no
    fallback.
    """
    if not model:
        return None, None

    from services import memory_pressure_monitor as mpm
    from services.ollama_service import DEFAULT_OLLAMA_BASE_URL, list_installed_models

    failed_entry = mpm.find_entry_by_ollama_model(model)
    if failed_entry is None:
        return None, None

    try:
        installed_raw = await list_installed_models(base_url or DEFAULT_OLLAMA_BASE_URL)
        tier = mpm.current_tier()
    except Exception:  # noqa: BLE001
        return None, None

    installed_tags = [m.get("name", "") for m in installed_raw if m.get("name")]
    ctx_len = ctx_len_tokens or failed_entry.effective_context_tokens

    swap = mpm.pick_runnable_model(
        failed_entry,
        tier=tier,
        ctx_len_tokens=ctx_len,
        installed_ollama_tags=installed_tags,
    )
    if swap.chosen is None or swap.chosen.id == failed_entry.id:
        return None, None

    warning = (
        f"Out of memory on {failed_entry.label or failed_entry.id} — "
        f"switched to {swap.chosen.label or swap.chosen.id}"
    )
    return swap.chosen.ollama_model, warning


DEFAULT_OLLAMA_MODEL = "qwen3:8b"


def _make_llm(provider: Optional[str], model: Optional[str], api_key: str, base_url: Optional[str] = None):
    """Construct the chat dispatcher for a turn.

    Per ADR 015 the v1 stack has a single dispatcher (`OllamaDispatcher`).
    The `provider` and `api_key` parameters survive on the signature so call
    sites stay mechanical — the values are ignored. The memory-pressure
    auto-downgrade lives upstream of this construction step (it picks
    *which* model to ask for) — see
    [`_apply_memory_pressure_swap`](memory_pressure_monitor.py); `_make_llm`
    itself stays a thin "build the configured client" helper.
    """
    from services.ollama_service import DEFAULT_OLLAMA_BASE_URL

    config = OllamaDispatchConfig(
        model=model or DEFAULT_OLLAMA_MODEL,
        api_base=base_url or DEFAULT_OLLAMA_BASE_URL,
    )
    return OllamaDispatcher(config)


def _resolve_system_prompt_budget(
    provider: Optional[str], model: Optional[str]
) -> tuple[Optional[int], Optional[str]]:
    """Compute the system-prompt token budget for the active model.

    Returns ``(budget_tokens, tokenizer_id)``. ``(None, None)`` is the
    safe fallback that opts the request out of budget enforcement —
    used for local models without a catalog entry (off-ladder dev tags).
    """
    if not model:
        return None, None
    from services.ollama_service import get_model_by_ollama_model
    from services.system_prompt import _SYSTEM_PROMPT_BUDGET_FRACTION

    entry = get_model_by_ollama_model(model)
    if entry is None:
        return None, None
    budget = int(entry.effective_context_tokens * _SYSTEM_PROMPT_BUDGET_FRACTION)
    return budget, entry.tokenizer_id


async def _maybe_compact(
    messages: list[dict],
    *,
    session_id: str,
    system_prompt: str,
    retrieval_block: str = "",
    provider: Optional[str],
    model: Optional[str],
    ws: WebSocket,
) -> list[dict]:
    """Apply ADR 009 production compaction.

    Returns the (possibly compacted) message list. Records an event on
    the session row when compaction fires; emits a ``compaction`` WS
    event so the frontend can surface "this turn was compacted" UI.

    Skipped for models not in the catalog (custom user pulls or off-ladder
    dev tags) — compaction needs the catalog's effective_context_tokens
    field to know when to fire.

    Per ADR 009 amendment 2026-05-01 the retrieval block lives in the
    user-message position rather than the system prompt, but it still
    counts toward the dispatched prefix size; pass it in so the headroom
    math (``effective_ctx - prefix - output_reserve``) reflects what
    Ollama will actually see, not just the system prompt.
    """
    if not model:
        return messages

    # Wrap the entire compaction path in one guard. Compaction is a
    # quality lift, not a correctness gate — any failure (catalog
    # lookup, tokenizer load, retrieval, recording, WS emission) must
    # degrade to "use uncompacted history" rather than abort the turn.
    # Without this wrapper, an unhandled exception here would propagate
    # to _handle_message which has no surrounding try, dropping the WS
    # connection silently.
    try:
        from services.ollama_service import get_model_by_ollama_model
        from services.compaction_service import compact_messages
        from services.token_counting import count_tokens

        entry = get_model_by_ollama_model(model)
        if entry is None:
            return messages

        system_prompt_tokens = count_tokens(system_prompt, tokenizer_id=entry.tokenizer_id)
        retrieval_tokens = (
            count_tokens(retrieval_block, tokenizer_id=entry.tokenizer_id)
            if retrieval_block else 0
        )

        result = await compact_messages(
            messages,
            effective_context_tokens=entry.effective_context_tokens,
            tokenizer_id=entry.tokenizer_id,
            system_prompt_tokens=system_prompt_tokens + retrieval_tokens,
            current_session_id=session_id,
        )

        if result.compacted:
            event = result.as_event()
            session_service.record_compaction_event(session_id, event)
            try:
                await _send_event(
                    ws, "compaction",
                    turns_dropped=result.turns_dropped,
                    recent_window_size=result.recent_window_size,
                    tokens_before=result.tokens_before,
                    tokens_after=result.tokens_after,
                    effective_ctx=result.effective_ctx,
                    retrieval_paths=[r.get("path", "") for r in result.retrieval_results],
                )
            except Exception:
                # WS may already be torn down — surface at INFO so an
                # operator monitoring the production logs can see the
                # client missed the compaction notification (the audit
                # event was already recorded above this).
                logger.info("Failed to emit compaction WS event for session %s", session_id)

        return result.messages
    except Exception:
        logger.exception("Compaction wiring failed for session %s; using uncompacted history", session_id)
        return messages


async def _handle_message(
    ws: WebSocket,
    session_id: str,
    content: str,
    get_llm: callable = None,
    graph_scope: Optional[str] = None,
    import_batch_id: Optional[str] = None,
    client_api_key: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    context_strategy: Optional[ContextStrategy] = None,
) -> None:
    # ADR 015 — `api_key` is unused by the dispatcher (single target, no
    # auth). Resolved here only because tool execution still threads it
    # through; blank when absent. (Per ADR 020 the v1 tool surface has
    # no outbound-key consumers; the parameter remains for forward-compat
    # with future tools that may need credentials.)
    api_key = client_api_key or get_api_key() or ""

    # Per-step latency probe (TEMPORARY) — investigation of build #5 turn 2
    # showing 25s end-to-end with Ollama reporting only 1.58s of work.
    # ~23s is being spent in the Python pipeline somewhere; capturing
    # elapsed-from-start at each major step pinpoints which one. To remove:
    # delete this block and the `_step` calls below (search "step_log").
    import time as _time
    _step_t0 = _time.perf_counter()
    def _step_log(step: str) -> None:
        elapsed_ms = (_time.perf_counter() - _step_t0) * 1000.0
        logger.info(
            "chat_step session=%s step=%s elapsed_ms=%.1f",
            session_id, step, elapsed_ms,
        )

    session_service.add_message(session_id, "user", content)
    _step_log("after_add_message")
    strategy = context_strategy or DEFAULT_STRATEGY
    messages = strategy.assemble(session_service.get_messages(session_id))
    _step_log("after_strategy_assemble")
    if not isinstance(messages, list):
        raise TypeError(
            f"ContextStrategy {strategy.name!r} returned {type(messages).__name__}; "
            "expected list. The strategy boundary rejects malformed returns "
            "to prevent confusing provider-level errors downstream."
        )

    # ADR 009 — derive system-prompt budget from the active model's
    # effective ceiling so the retrieved-context block can be capped if a
    # huge retrieval would push the prompt past the model's safe window.
    sp_budget_tokens, sp_tokenizer_id = _resolve_system_prompt_budget(provider, model)
    _step_log("after_resolve_budget")

    system_prompt, prompt_stats = await build_system_prompt_with_stats(
        content,
        graph_scope=graph_scope,
        import_batch_id=import_batch_id,
        system_prompt_budget_tokens=sp_budget_tokens,
        tokenizer_id=sp_tokenizer_id,
    )
    _step_log("after_build_system_prompt")

    # ADR 009 §"Atomicity" — compaction runs ONLY here at the per-turn
    # boundary, never inside _stream_follow_up. Triggered when projected
    # tokens exceed `threshold_pct × effective_ctx_tokens`. No-ops when
    # the active model has no catalog entry (off-ladder dev tags).
    #
    # Note: compaction runs against the *clean* history (user messages
    # without the per-turn retrieval glue). Retrieval is attached AFTER
    # compaction so old turns get folded into the recall block without
    # carrying stale retrieval forward.
    messages = await _maybe_compact(
        messages,
        session_id=session_id,
        system_prompt=system_prompt,
        retrieval_block=prompt_stats.get("retrieval_block", "") or "",
        provider=provider,
        model=model,
        ws=ws,
    )
    _step_log("after_maybe_compact")

    # ADR 009 amendment 2026-05-01 — retrieval lives in the user-message
    # position so the system prompt stays byte-identical across turns and
    # Ollama's KV cache prefix-match reuses the long stable prefix on
    # warm follow-ups. Attach the just-built retrieval block onto the
    # latest user message in `messages` before dispatch. No-op when
    # retrieval was empty.
    messages = attach_retrieval_to_user_message(
        messages, prompt_stats.get("retrieval_block", "") or ""
    )
    _step_log("after_attach_retrieval")
    active_specs = specialist_service.get_active_specialists()
    tools = specialist_service.filter_tools(TOOLS, specialists=active_specs)
    _step_log("after_specialists_filter")
    # Memory-pressure pre-flight swap (ADR 005 §C trigger 2). Runs before
    # _make_llm so the constructed dispatcher points at a model that
    # actually fits in current free RAM.
    swapped_model, floor_refused = await _apply_memory_pressure_swap(
        ws=ws,
        provider=provider,
        model=model,
        base_url=base_url,
        ctx_len_tokens=prompt_stats.get("context_tokens"),
    )
    _step_log("after_pressure_swap")
    # `floor_refused` is now structurally always False — the auto-downgrade
    # pre-flight check was removed (see _apply_memory_pressure_swap). Kept
    # for callsite stability; if Ollama actually OOMs at load we walk the
    # ladder via _ladder_step_after_oom downstream.
    assert not floor_refused
    model = swapped_model

    claude = get_llm(api_key or "", provider, model, base_url) if get_llm else _make_llm(provider, model, api_key or "", base_url)
    assistant_text = ""
    # Specialist attribution: prefix first text_delta with specialist names
    attribution_prefix = ""
    # Step 28e: pass the active specialist ID to tool execution so client-estimator
    # can route search_notes through the hybrid pipeline.
    active_specialist_id = active_specs[0]["id"] if active_specs else ""
    if active_specs:
        names = ", ".join(s["name"] for s in active_specs)
        attribution_prefix = f"**[{names}]** "
    # [input_tokens, output_tokens] accumulated across all rounds
    usage_acc = [0, 0]
    # [tool_calls, tool_rounds] accumulated across all rounds
    tool_acc = [0, 0]
    # Per-stage timings (decode, prefill, load, total) — see _new_metrics_acc
    metrics_acc = _new_metrics_acc()
    pending_tools: list[StreamEvent] = []

    # OOM-retry loop (ADR 005 §C trigger 1). Up to one walk-the-ladder
    # retry, only if Ollama errors with an OOM signature *before* any
    # text streams. Once text has been emitted, we don't retry — the user
    # already saw output and a re-stream would double up.
    from services import memory_pressure_monitor as mpm
    text_started = False
    oom_retry_done = False
    _step_log("before_stream_response")
    # Per-frame send timing — investigation of the consistently-reproducing
    # 24 s back-pressure on turn 2 of every fresh app open. If individual
    # text_delta sends are >100 ms each, the WebSocket / frontend webview
    # is the bottleneck (frames pile up faster than the renderer can drain).
    # If individual sends are fast but with long gaps between them, it's
    # something else in this loop (e.g. waiting on the Ollama stream).
    # Logged as one summary line at end of stream so we don't flood logs.
    _send_durations: list[float] = []
    _send_slow_count = 0
    _send_max_ms = 0.0
    while True:
        saw_oom_pre_text = False
        async for event in claude.stream_response(
            messages=messages,
            system_prompt=system_prompt,
            tools=tools,
        ):
            if event.type == "text_delta":
                text_started = True
                content = event.content
                if attribution_prefix:
                    content = attribution_prefix + content
                    attribution_prefix = ""  # Only prepend once
                assistant_text += content
                _send_t0 = _time.perf_counter()
                await _send_event(ws, "text_delta", content=content)
                _send_dur_ms = (_time.perf_counter() - _send_t0) * 1000.0
                _send_durations.append(_send_dur_ms)
                if _send_dur_ms > _send_max_ms:
                    _send_max_ms = _send_dur_ms
                if _send_dur_ms > 100.0:
                    _send_slow_count += 1

            elif event.type == "tool_use":
                pending_tools.append(event)

            elif event.type == "usage":
                usage_acc[0] += event.input_tokens
                usage_acc[1] += event.output_tokens
                _update_metrics_acc(metrics_acc, event)

            elif event.type == "error":
                if (
                    provider == "ollama"
                    and not text_started
                    and not oom_retry_done
                    and mpm.looks_like_oom(event.content or "")
                ):
                    saw_oom_pre_text = True
                    break
                await _send_event(ws, "error", content=event.content)

        if not saw_oom_pre_text:
            break

        oom_retry_done = True
        new_model, warning = await _ladder_step_after_oom(
            provider=provider,
            model=model,
            base_url=base_url,
            ctx_len_tokens=prompt_stats.get("context_tokens"),
        )
        if new_model is None:
            await _send_event(
                ws,
                "error",
                content="Out of memory and no smaller installed model fits. Free up RAM and try again.",
            )
            break
        # Reset the metrics accumulator before retrying. If Ollama emitted
        # a `usage` event with partial timings before the OOM error in the
        # failed round (uncommon but possible — partial-chunk abort path),
        # those timings belong to the *evicted* model. Carrying them into
        # the retried round's `done.metrics` would attribute the failed
        # model's load+prefill to the smaller fallback and feed a
        # mis-attributed sample to the chat-health watcher.
        metrics_acc = _new_metrics_acc()
        model = new_model
        await _send_event(ws, "warning", content=warning or "Switched to smaller model after OOM")
        claude = (
            get_llm(api_key or "", provider, model, base_url)
            if get_llm else
            _make_llm(provider, model, api_key or "", base_url)
        )
        # Loop back: retry the stream against the smaller model.

    # Handle tool call chain (up to MAX_TOOL_ROUNDS)
    if pending_tools:
        tool_messages = messages
        tool_acc[0] += len(pending_tools)
        tool_acc[1] += 1
        for tool_event in pending_tools:
            await _send_event(ws, "tool_use", name=tool_event.name, input=tool_event.tool_input)
            session_service.record_tool_use(session_id, tool_event.name)
            result = await _run_tool(tool_event, session_id=session_id, api_key=api_key, specialist_id=active_specialist_id)
            await _send_event(ws, "tool_result", name=tool_event.name, content=result)
            await _emit_memory_changed(ws, tool_event.name, tool_event.tool_input or {})
            tool_messages = _build_tool_messages(tool_messages, tool_event, result)

        assistant_text += await _stream_follow_up(
            ws, claude, tool_messages, system_prompt, tools,
            session_id, api_key, usage_acc, tool_acc=tool_acc,
            specialist_id=active_specialist_id, metrics_acc=metrics_acc,
        )

    if assistant_text:
        session_service.add_message(
            session_id, "assistant", assistant_text,
            model=model or "claude-sonnet-4-20250514",
            provider=provider or "ollama",
        )
    else:
        # No text response (e.g. pure tool-use) — still persist so session
        # state stays consistent, but add_message already auto-saves on the
        # user message so an extra save here is only needed when we have text.
        pass

    # Log token usage if we got any
    if usage_acc[0] > 0 or usage_acc[1] > 0:
        try:
            log_usage(
                usage_acc[0], usage_acc[1],
                model=model or "claude-sonnet-4-20250514",
                provider=provider or "ollama",
                context_tokens=prompt_stats.get("context_tokens", 0),
                tool_calls=tool_acc[0],
                tool_rounds=tool_acc[1],
            )
        except Exception:
            logger.warning("Failed to log token usage")

    done_fields = {
        "session_id": session_id,
        "model": model or "claude-sonnet-4-20250514",
        "provider": provider or "ollama",
    }
    # Per-turn telemetry (ADR 005 §C trigger 2) — forwarded only when
    # any round in this turn reported timings. The frontend attaches
    # the payload to the just-finished assistant message and feeds it
    # to the chat-health watcher for the observed-vs-baseline ratio.
    metrics_payload = _build_metrics_payload(metrics_acc)
    if metrics_payload is not None:
        done_fields["metrics"] = metrics_payload

    _step_log("after_stream_complete")

    # Per-frame WS send-timing summary. Total = sum of every
    # `await ws.send_json(text_delta)`; max = slowest single send;
    # slow_count = # of sends that exceeded 100 ms. If total ≈ stream
    # wall-clock, the WebSocket is fully back-pressuring (the frontend
    # can't drain frames fast enough). If total ≪ stream wall-clock,
    # something else in the streaming loop is blocking.
    if _send_durations:
        _send_total_ms = sum(_send_durations)
        logger.info(
            "chat_step session=%s step=stream_send count=%d total_ms=%.1f max_ms=%.1f slow_count=%d",
            session_id, len(_send_durations), _send_total_ms, _send_max_ms, _send_slow_count,
        )

    # Prefill-cost diagnostic — fires whether or not Ollama returned timings,
    # so we can also detect "all rounds aborted before usage" cases. See
    # `_prefill_log` for the hypothesis under test.
    try:
        _prefill_log(
            session_id=session_id,
            system_prompt=system_prompt,
            prompt_stats=prompt_stats,
            metrics_acc=metrics_acc,
            tool_acc=tool_acc,
            model=model,
        )
    except Exception:  # noqa: BLE001 — diagnostic must never break the turn
        logger.debug("prefill_log failed", exc_info=True)

    # Include tool_mode for local models so the frontend can show tool support info
    if provider == "ollama" and model:
        from services.ollama_service import MODEL_CATALOG, _tool_mode_for
        # Strip a stale `ollama_chat/` prefix in case the WS payload is from a
        # client that still has the old default in localStorage. Cleaned forms
        # pass through unchanged.
        ollama_name = model.replace("ollama_chat/", "", 1) if model.startswith("ollama_chat/") else model
        for entry in MODEL_CATALOG:
            if entry.ollama_model == ollama_name:
                done_fields["tool_mode"] = _tool_mode_for(entry)
                break
        else:
            done_fields["tool_mode"] = "adapted"

    # Step 28a — surface the per-note retrieval trace before `done`. Older
    # clients ignore unknown event types, so this degrades cleanly.
    trace_items = prompt_stats.get("trace") or []
    if trace_items:
        await _send_event(ws, "trace", items=trace_items)

    await _send_event(ws, "done", **done_fields)

    # Save conversation to memory after every assistant reply.
    # First save happens after the first exchange; subsequent saves update the
    # same note (dedup by session_id in frontmatter).
    _schedule_session_save(session_id)


def _validate_message_dict(data: dict) -> tuple:
    """Validate a parsed chat-message dict. Returns (data, error_message).
    Returns (None, None) for control messages like ping that should be silently ignored.
    """
    if data.get("type") == "ping":
        return None, None
    content = data.get("content", "").strip()
    if not content:
        return None, "Message content is required"
    return data, None


def _parse_message(raw: str) -> tuple:
    """Parse raw WS text. Returns (data, error_message).
    Returns (None, None) for control messages like ping that should be silently ignored.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None, "Invalid JSON"
    return _validate_message_dict(data)


# ── Session → WebSocket registry ──────────────────────────────────────────
#
# Maps session_id to the active WebSocket + that connection's per-session
# get_llm closure + a per-session asyncio.Lock that prevents two concurrent
# `_handle_message` runs from corrupting session state.
#
# Used by the HTTP `POST /api/chat/message` endpoint (ADR 016) to dispatch
# inbound user messages onto the same WebSocket the frontend is already
# listening to. The HTTP endpoint exists because macOS WKWebView can throttle
# the WebView's outbound WebSocket frames after the view goes idle (measured
# at ~27 s wire_time on M5 turn 2). Routing sends through the Tauri Rust
# shell over HTTP bypasses that throttling entirely. Streaming responses
# still flow back over the WebSocket — the registry is the bridge.
#
# Lifecycle: registered when the WS handler accepts a session, removed in
# the WS handler's `finally` block. Re-registered if the client switches
# session_id mid-connection (rare; happens on reconnect-with-prior-id when
# the active session_id from query-param resume differs from a payload
# `session_id` field). Concurrent same-session sends serialise on `lock`.
_active_sessions: "dict[str, dict]" = {}


async def _process_chat_payload(
    websocket: WebSocket,
    session_id: str,
    data: dict,
    get_llm,
    transport: str = "ws",
) -> str:
    """Run the wire-time diagnostic + `_handle_message` for a validated
    payload. Used by both the WS receive loop and the HTTP dispatch
    endpoint. Returns the (possibly updated) session_id — the caller is
    responsible for re-registering in `_active_sessions` if it changed.
    """
    client_api_key = data.get("api_key") or None
    client_provider = data.get("provider") or None
    client_model = data.get("model") or None
    client_base_url = data.get("base_url") or None
    content = data.get("content", "").strip()

    requested_sid = data.get("session_id")
    if requested_sid and requested_sid != session_id:
        if session_service.get_session(requested_sid):
            session_id = requested_sid

    graph_scope = data.get("graph_scope") or None
    import_batch_id = data.get("import_batch_id") or None

    # Wire-time diagnostic — `wire_time_ms` is the headline number
    # (now - t_pre_send), and the three sub-fields decompose where the
    # delay actually lives:
    #
    #   js_to_rust_ms      — t_rust_received - t_pre_send
    #     JS event-loop scheduling + Tauri JS-to-Rust IPC bridge.
    #     Large values point at WKWebView/JSContext throttling
    #     (e.g. App Nap waking the bridge cold) — the same class of
    #     issue ADR 016 was meant to dodge by routing sends off the WS,
    #     surfacing here on the IPC bridge itself.
    #   rust_to_fastapi_ms — t_fastapi_entered - t_rust_received
    #     Rust reqwest HTTP roundtrip to loopback + macOS network stack.
    #     Should be <10 ms for a warm connection-pool. Large values
    #     point at connection-establishment overhead or kernel-side
    #     back-pressure.
    #   fastapi_to_lock_ms — now - t_fastapi_entered
    #     FastAPI route handler + per-session lock acquisition. Pre-WS
    #     send queue (this fix) this could be 20+ s waiting for the
    #     previous turn's stuck `_send_event(done)` to flush; should now
    #     be <5 ms because the lock is no longer held through WS writes.
    #
    # WS transport (browser dev / legacy) doesn't have t_rust or
    # t_fastapi (no Tauri / FastAPI route in the path), so those fields
    # render as `-` to keep the line greppable.
    try:
        _t_enter = data.pop("t_enter_ms", None)
        _t_pre_send = data.pop("t_pre_send_ms", None)
        _t_rust = data.pop("t_rust_received_ms", None)
        _t_fastapi = data.pop("_t_fastapi_entered_ms", None)
        if isinstance(_t_enter, (int, float)) and isinstance(_t_pre_send, (int, float)):
            import time as _time2
            _now_ms = _time2.time() * 1000.0
            _js_block_ms = float(_t_pre_send) - float(_t_enter)
            _wire_time_ms = _now_ms - float(_t_pre_send)

            def _fmt(value, base):
                if isinstance(value, (int, float)) and isinstance(base, (int, float)):
                    return f"{float(value) - float(base):.1f}"
                return "-"

            _js_to_rust = _fmt(_t_rust, _t_pre_send)
            _rust_to_fastapi = _fmt(_t_fastapi, _t_rust)
            _fastapi_to_lock = _fmt(_now_ms, _t_fastapi)

            logger.info(
                "chat_step session=%s step=received js_block_ms=%.1f js_to_rust_ms=%s rust_to_fastapi_ms=%s fastapi_to_lock_ms=%s wire_time_ms=%.1f transport=%s",
                session_id, _js_block_ms,
                _js_to_rust, _rust_to_fastapi, _fastapi_to_lock,
                _wire_time_ms, transport,
            )
    except Exception:
        pass

    await _handle_message(
        websocket, session_id, content, get_llm,
        graph_scope=graph_scope,
        import_batch_id=import_batch_id,
        client_api_key=client_api_key,
        provider=client_provider, model=client_model,
        base_url=client_base_url,
    )
    return session_id


@router.post("/message", dependencies=[Depends(require_functional)])
async def http_chat_message(request: Request) -> dict:
    """Inbound message dispatch over HTTP — the Tauri shell calls this from
    a Rust `#[tauri::command]` to bypass WKWebView's WebSocket throttling
    on macOS (ADR 016). The streaming response still goes over the WebSocket
    that the frontend is already listening on — we look that WS up via
    `_active_sessions` and dispatch the same `_handle_message` flow.

    Returns 200 immediately; the actual streaming runs as a background task
    so the HTTP request can complete without blocking the user's UI for the
    duration of the model response. Errors during processing surface to the
    frontend as `error` events on the existing WebSocket, matching the
    legacy WS-direct path's error shape.
    """
    # Wire-time decomposition: stamp now() the moment FastAPI's route
    # handler enters, BEFORE `await request.json()` (which itself can be
    # non-trivial on large payloads). Combined with t_pre_send_ms from JS
    # and t_rust_received_ms from the Tauri command, this lets
    # `_process_chat_payload` decompose wire_time_ms into the three legs
    # of the JS → Rust → FastAPI → lock pipeline.
    import time as _time
    t_fastapi_entered_ms = _time.time() * 1000.0

    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    payload["_t_fastapi_entered_ms"] = t_fastapi_entered_ms

    session_id = (payload.get("session_id") or "").strip()
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id required")

    entry = _active_sessions.get(session_id)
    if entry is None:
        # No active WS for this session. Either the WS hasn't connected yet
        # or it just closed. Frontend should reconnect and retry.
        raise HTTPException(
            status_code=503,
            detail=f"No active WebSocket for session {session_id}",
        )

    data, error = _validate_message_dict(payload)
    if error:
        # Surface as a WS event so the chat UI shows the error inline,
        # matching the WS-path error contract. HTTP 200 because we did
        # acknowledge the message — the failure is semantic, not transport.
        try:
            await _send_event(entry["ws"], "error", content=error)
        except Exception:
            pass
        return {"ok": False, "error": error}
    if data is None:
        # Heartbeat-style ping arriving via HTTP — no-op.
        return {"ok": True, "skipped": "ping"}

    # Dispatch as a background task so the HTTP request returns immediately.
    # The streaming response goes back over the WS independently.
    async def _dispatch():
        try:
            async with entry["lock"]:
                new_sid = await _process_chat_payload(
                    entry["ws"], session_id, data, entry["get_llm"], transport="http",
                )
                if new_sid != session_id:
                    # Re-register under the new session_id (rare path).
                    _active_sessions.pop(session_id, None)
                    _active_sessions[new_sid] = entry
        except Exception:
            logger.exception("HTTP dispatch failed for session %s", session_id)

    asyncio.create_task(_dispatch())
    return {"ok": True}


@router.websocket("/ws")
async def chat_ws(websocket: WebSocket) -> None:
    # Origin allowlist check — CORSMiddleware does not cover WebSocket
    # handshakes, so enforce it manually to prevent cross-site WS hijacking.
    from config import get_settings as _get_settings
    origin = websocket.headers.get("origin")
    allowed_origins = _get_settings().cors_origins
    if origin is not None and origin not in allowed_origins:
        await websocket.close(code=1008)
        return

    await websocket.accept()

    # Attach the per-WS outbound send queue + start its consumer task
    # before any `_send_event(...)` fires below. All subsequent WS
    # writes go through the queue, so the per-session dispatch lock is
    # never held hostage by `ws.send_json` back-pressure (e.g. macOS
    # WKWebView App Nap between turns).
    send_queue = _attach_send_queue(websocket)

    # Allow resuming an existing session via query param (e.g. after reconnect)
    resume_id = websocket.query_params.get("session_id", "").strip()
    # Validate format before any lookup to prevent abuse
    _valid_resume = bool(resume_id and session_service.is_valid_session_id(resume_id))
    if _valid_resume and session_service.get_session(resume_id):
        # Session still in memory — just reattach
        session_id = resume_id
    elif _valid_resume:
        # Session was saved to disk but cleared from memory — try to reload
        try:
            session_id = session_service.resume_session(resume_id)
        except Exception:
            session_id = session_service.create_session()
    else:
        session_id = session_service.create_session()

    await _send_event(websocket, "session_start", session_id=session_id)

    # Send existing messages so the frontend can restore chat history after refresh
    existing = session_service.get_messages(session_id)
    if existing:
        await _send_event(websocket, "session_history", messages=existing)

    # Reuse a single LLM service per connection to avoid per-message HTTP pool churn.
    # Cache is keyed on (api_key, provider, model, base_url) — changed key/provider
    # creates a new instance.
    _connection_llm = None
    _connection_llm_key: tuple = (None, None, None, None)

    def _get_llm(api_key: str, provider: Optional[str] = None, model: Optional[str] = None, base_url: Optional[str] = None):
        nonlocal _connection_llm, _connection_llm_key
        cache_key = (api_key, provider, model, base_url)
        if _connection_llm is None or _connection_llm_key != cache_key:
            _connection_llm = _make_llm(provider, model, api_key, base_url)
            _connection_llm_key = cache_key
        return _connection_llm

    # Register this WS in `_active_sessions` so HTTP dispatch (the Tauri-IPC
    # path used to bypass WKWebView's outbound throttling) can route inbound
    # user messages onto this socket. Lock prevents two concurrent
    # `_handle_message` runs for the same session — applies whether messages
    # arrive over WS or HTTP.
    _handler_lock = asyncio.Lock()
    _active_sessions[session_id] = {
        "ws": websocket,
        "get_llm": _get_llm,
        "lock": _handler_lock,
    }

    try:
        while True:
            raw = await websocket.receive_text()
            data, error = _parse_message(raw)

            # Heartbeat ping → echo a pong. Without this echo the frontend
            # has no inbound liveness signal during idle, so a TCP socket
            # that died silently (App Nap, OS sleep, NAT timeout) stays in
            # readyState=OPEN at the JS layer. The next ws.send() then
            # appears to succeed but stalls in TCP retransmits — measured
            # at ~23 s end-to-end on M5 after a multi-hour idle gap. With
            # the pong echo, the frontend can detect staleness on the
            # next send and force-reconnect instead of trusting readyState.
            if data is None and error is None:
                try:
                    await _send_event(websocket, "pong")
                except Exception:
                    pass
                continue

            if error:
                await _send_event(websocket, "error", content=error)
                continue

            async with _handler_lock:
                new_sid = await _process_chat_payload(
                    websocket, session_id, data, _get_llm, transport="ws",
                )
                if new_sid != session_id:
                    _active_sessions.pop(session_id, None)
                    _active_sessions[new_sid] = {
                        "ws": websocket,
                        "get_llm": _get_llm,
                        "lock": _handler_lock,
                    }
                    session_id = new_sid

    except WebSocketDisconnect:
        # Cancel any pending debounced save — we'll do a final save now
        old_task = _pending_saves.pop(session_id, None)
        if old_task and not old_task.done():
            old_task.cancel()

        session_service.save_session(session_id)
        try:
            await session_service.save_session_to_memory(session_id)
        except Exception:
            logger.exception("Failed to save session %s to memory", session_id)
        # Don't delete the session from memory — it may be resumed on reconnect.
        # Sessions are cleaned up when a new session is explicitly created or
        # the server restarts.
    finally:
        # Unregister from the active-sessions map. Pop by current session_id;
        # also pop any other entries that point at THIS websocket so a
        # reconnect under a different id can't leave a dangling reference.
        _active_sessions.pop(session_id, None)
        for _sid, _entry in list(_active_sessions.items()):
            if _entry.get("ws") is websocket:
                _active_sessions.pop(_sid, None)
        # Stop the WS send queue's consumer task. Drain pending events
        # for up to 1s — most cases are clean; a stuck client gets the
        # consumer cancelled so we don't block the WS handler's exit.
        try:
            await send_queue.close()
        finally:
            _detach_send_queue(websocket)
        if _connection_llm is not None:
            try:
                await _connection_llm.close()
            except Exception:
                pass
