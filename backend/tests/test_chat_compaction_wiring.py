"""Tests for the chat-router-level wiring of ADR 009 production compaction.

Covers the two router-side helpers that connect the compaction service
to the WebSocket turn lifecycle:

- ``_resolve_system_prompt_budget``: returns ``(budget_tokens,
  tokenizer_id)`` for local Ollama models, ``(None, None)`` otherwise.
  Wrong values here mean the system-prompt budget enforcement either
  kicks in for the wrong models or fails to kick in when it should.
- ``_maybe_compact``: wraps ``compact_messages``, records audit events
  on the session row, and emits a ``compaction`` WS event. Wrong
  behaviour here means compliance buyers don't see an audit trail.
"""

from __future__ import annotations

import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import pytest

from routers import chat as chat_router
from services import session_service


pytestmark = pytest.mark.anyio(backends=["asyncio"])


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


@pytest.fixture(autouse=True)
def _isolate_sessions():
    session_service._sessions.clear()
    yield
    session_service._sessions.clear()


class _SpyWebSocket:
    """Minimal WS double — captures send_json payloads for assertions."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)


# ── _resolve_system_prompt_budget ────────────────────────────────────


def test_resolve_budget_returns_none_for_anthropic():
    budget, tok = chat_router._resolve_system_prompt_budget(
        provider="anthropic", model="claude-sonnet-4",
    )
    assert budget is None
    assert tok is None


def test_resolve_budget_returns_none_for_unknown_local_model():
    budget, tok = chat_router._resolve_system_prompt_budget(
        provider="ollama", model="mystery:99b",
    )
    assert budget is None
    assert tok is None


def test_resolve_budget_returns_value_for_known_local_model():
    """Qwen3 8B is in the catalog at 32K effective context; budget is 30%."""
    budget, tok = chat_router._resolve_system_prompt_budget(
        provider="ollama", model="qwen3:8b",
    )
    assert budget is not None
    # 32_768 × 0.30 = 9830
    assert 9000 <= budget <= 11000
    assert tok == "Qwen/Qwen3-8B"


# ── _maybe_compact ───────────────────────────────────────────────────


async def test_maybe_compact_noop_for_cloud_provider():
    ws = _SpyWebSocket()
    sid = session_service.create_session()
    messages = [{"role": "user", "content": "hi"}]
    out = await chat_router._maybe_compact(
        messages,
        session_id=sid,
        system_prompt="sys",
        provider="anthropic",
        model="claude-sonnet-4",
        ws=ws,
    )
    assert out == messages
    assert ws.sent == []
    assert session_service.get_compaction_events(sid) == []


async def test_maybe_compact_noop_when_under_threshold():
    ws = _SpyWebSocket()
    sid = session_service.create_session()
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi back"},
    ]
    out = await chat_router._maybe_compact(
        messages,
        session_id=sid,
        system_prompt="short prompt",
        provider="ollama",
        model="qwen3:8b",
        ws=ws,
    )
    # Under threshold → original messages preserved, no audit event,
    # no WS event.
    assert out == messages
    assert ws.sent == []
    assert session_service.get_compaction_events(sid) == []


async def test_maybe_compact_records_event_and_emits_ws_when_over_threshold(monkeypatch):
    ws = _SpyWebSocket()
    sid = session_service.create_session()

    async def _fake_vault(*args, **kwargs):
        return [{"path": "conversations/old.md", "title": "old", "snippet": "buried"}]

    monkeypatch.setattr(
        "services.retrieval.sessions.find_earlier_turn_context", _fake_vault,
    )
    # Force a tiny effective ceiling on the catalog entry the test will
    # use so the trigger fires deterministically without needing a
    # 32K-token fixture.
    # Force a small (but not impossibly small) effective ceiling so the
    # 70% threshold fires on this test fixture without the default 4096
    # output reserve eating the entire budget.
    from services.ollama_service import _CATALOG_BY_ID
    qwen8b = _CATALOG_BY_ID["qwen3-8b"]
    monkeypatch.setattr(qwen8b, "effective_context_tokens", 5000)

    messages: list[dict] = []
    for i in range(15):
        messages.append({"role": "user", "content": "user question " + "x" * 200 + f" iter{i}"})
        messages.append({"role": "assistant", "content": "assistant reply " + "y" * 200 + f" iter{i}"})

    out = await chat_router._maybe_compact(
        messages,
        session_id=sid,
        system_prompt="sys" * 10,
        provider="ollama",
        model="qwen3:8b",
        ws=ws,
    )
    # Strategy dropped older turns; recent_n default is 8 → 16 messages
    # remain (8 user/assistant pairs) plus the synthesized substitution
    # block at index 0.
    assert len(out) <= len(messages)
    assert out[0]["role"] == "assistant"  # substitution block (see compaction_service._synthesize_retrieval_block)
    assert "buried" in out[0]["content"]
    assert out[1]["role"] == "user"  # alternation preserved into the kept window

    events = session_service.get_compaction_events(sid)
    assert len(events) == 1
    e = events[0]
    assert e["turns_dropped"] >= 1
    assert e["recent_window_size"] == 8
    assert e["effective_ctx_at_event"] == 5000
    assert "conversations/old.md" in e["retrieval_paths"]
    assert e["reason"] == "compacted"
    assert "timestamp" in e

    # WS got a compaction event with the same numbers.
    compaction_msgs = [m for m in ws.sent if m.get("type") == "compaction"]
    assert len(compaction_msgs) == 1
    cm = compaction_msgs[0]
    assert cm["turns_dropped"] == e["turns_dropped"]
    assert cm["recent_window_size"] == 8
    assert cm["retrieval_paths"] == ["conversations/old.md"]


async def test_maybe_compact_internal_failure_returns_uncompacted(monkeypatch):
    """If the compaction service raises, the turn must continue with the
    uncompacted history rather than failing — compaction is a quality
    lift, not a correctness gate."""
    ws = _SpyWebSocket()
    sid = session_service.create_session()

    async def _exploding_compact(*args, **kwargs):
        raise RuntimeError("compact boom")

    monkeypatch.setattr(
        "services.compaction_service.compact_messages",
        _exploding_compact,
    )

    messages = [{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}]
    out = await chat_router._maybe_compact(
        messages,
        session_id=sid,
        system_prompt="sys",
        provider="ollama",
        model="qwen3:8b",
        ws=ws,
    )
    assert out == messages
    assert session_service.get_compaction_events(sid) == []


# ── Audit-event persistence round-trip ────────────────────────────────


def test_compaction_event_persisted_in_session_save_and_resumed(tmp_path):
    """An event recorded mid-conversation must round-trip through
    save_session → resume_session intact. Compliance buyers expect the
    audit trail to survive process restarts."""
    sid = session_service.create_session()
    session_service.add_message(sid, "user", "hello")
    session_service.add_message(sid, "assistant", "hi")
    event = {
        "timestamp": "2026-04-29T12:00:00+00:00",
        "turns_dropped": 4,
        "summary_used": False,
        "recent_window_size": 8,
        "effective_ctx_at_event": 32768,
        "tokens_before": 20000,
        "tokens_after": 4500,
        "threshold_pct": 0.70,
        "retrieval_paths": ["conversations/old.md"],
        "reason": "compacted",
    }
    session_service.record_compaction_event(sid, event)

    (tmp_path / "app" / "sessions").mkdir(parents=True)
    session_service.save_session(sid, tmp_path)

    session_service._sessions.clear()
    session_service.resume_session(sid, tmp_path)

    events = session_service.get_compaction_events(sid)
    assert events == [event]
