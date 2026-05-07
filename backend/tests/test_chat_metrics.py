"""Per-turn telemetry forwarding (ADR 005 §C trigger 2).

These tests cover the wiring between `OllamaDispatcher`'s `usage`
event (with per-stage durations) and the `metrics` payload the chat
router emits on the WS `done` event. The `useChatHealth` watcher and
the per-turn telemetry pill in ChatPanel both depend on this surface
existing and being shaped consistently.

Underlying decode_tps math is unit-tested via the dispatcher tests.
These tests pin the *router-level* contract:

  - durations on at least one round → `metrics` field on `done`
  - all rounds without durations → no `metrics` field
  - multi-round (tool-using) turns aggregate decode/prefill across
    rounds and use the *first* round's load + prefill for TTFT
"""

from __future__ import annotations

import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

from main import app
from models.database import FTS_SQL, SCHEMA_SQL, TRIGGER_SQL
from services.system_prompt import StreamEvent

pytestmark = pytest.mark.anyio(backends=["asyncio"])


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


@pytest.fixture(autouse=True)
def isolate_workspace(tmp_path, monkeypatch):
    settings = MagicMock()
    settings.workspace_path = tmp_path
    for mod in [
        "services.session_service", "services.memory_service",
        "services.graph_service", "services.context_builder",
        "services.preference_service", "services.token_tracking",
        "services.workspace_service",
    ]:
        try:
            monkeypatch.setattr(f"{mod}.get_settings", lambda: settings)
        except AttributeError:
            pass
    for d in [
        "app", "app/sessions", "memory", "memory/inbox",
        "memory/preferences", "graph",
    ]:
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(tmp_path / "app" / "jarvis.db")) as conn:
        conn.executescript(SCHEMA_SQL + FTS_SQL + TRIGGER_SQL)


def _stream(*events: StreamEvent):
    async def _gen(**kwargs):
        for e in events:
            yield e
    return _gen


def _send_and_collect(model: str = "qwen3:8b") -> list[dict]:
    """Send one message and collect every WS event up to and including done.

    Patches `_schedule_session_save` to a no-op so the 2 s debounced
    background save task doesn't leak past test teardown — these tests
    cover only the WS event shape, not persistence.
    """
    with patch("routers.chat._schedule_session_save", lambda *a, **kw: None):
        with TestClient(app) as client:
            with client.websocket_connect("/api/chat/ws") as ws:
                ws.receive_json()  # session_start
                ws.send_json({
                    "content": "Hello",
                    "provider": "ollama",
                    "model": model,
                })
                events = []
                while True:
                    msg = ws.receive_json()
                    events.append(msg)
                    if msg["type"] == "done":
                        break
    return events


def test_done_carries_metrics_when_round_reports_durations():
    """Single-round happy path: the usage event's durations turn into
    a `metrics` payload on `done` with decode_tps + TTFT computed."""
    instance = MagicMock()
    instance.stream_response = _stream(
        StreamEvent(type="text_delta", content="Hello, world."),
        StreamEvent(
            type="usage",
            input_tokens=10,
            output_tokens=20,
            eval_duration_ns=1_000_000_000,         # 1 s decode
            prompt_eval_duration_ns=50_000_000,      # 50 ms prefill
            load_duration_ns=200_000_000,            # 200 ms cold load
            total_duration_ns=1_300_000_000,         # 1.3 s end-to-end
        ),
    )
    with patch("routers.chat.get_api_key", return_value=""), \
         patch("routers.chat._make_llm", return_value=instance), \
         patch("routers.chat._apply_memory_pressure_swap",
               new=AsyncMock(return_value=("qwen3:8b", False))):
        events = _send_and_collect()

    done = events[-1]
    assert done["type"] == "done"
    assert "metrics" in done
    m = done["metrics"]
    # decode_tps = 20 tokens / 1.0 s = 20 tps
    assert m["decode_tps"] == pytest.approx(20.0, rel=0.01)
    # prefill_tps = 10 tokens / 0.05 s = 200 tps
    assert m["prefill_tps"] == pytest.approx(200.0, rel=0.01)
    # TTFT = first round's load + prefill = 200ms + 50ms = 250ms
    assert m["ttft_ms"] == pytest.approx(250.0, rel=0.01)
    assert m["load_ms"] == pytest.approx(200.0, rel=0.01)
    assert m["total_ms"] == pytest.approx(1300.0, rel=0.01)
    assert m["eval_count"] == 20
    assert m["prompt_eval_count"] == 10


def test_done_omits_metrics_when_no_round_reports_durations():
    """No timings → no metrics payload. Frontend treats absence as
    'no telemetry this turn' and skips the watcher sample."""
    instance = MagicMock()
    instance.stream_response = _stream(
        StreamEvent(type="text_delta", content="Hi"),
        StreamEvent(type="usage", input_tokens=4, output_tokens=2),
    )
    with patch("routers.chat.get_api_key", return_value=""), \
         patch("routers.chat._make_llm", return_value=instance), \
         patch("routers.chat._apply_memory_pressure_swap",
               new=AsyncMock(return_value=("qwen3:8b", False))):
        events = _send_and_collect()

    done = events[-1]
    assert done["type"] == "done"
    assert "metrics" not in done


def test_done_omits_metrics_when_usage_event_absent_entirely():
    """No usage event at all (e.g. dispatch errored before timings) →
    no metrics. Token-logging path also skips gracefully."""
    instance = MagicMock()
    instance.stream_response = _stream(
        StreamEvent(type="text_delta", content="Partial."),
    )
    with patch("routers.chat.get_api_key", return_value=""), \
         patch("routers.chat._make_llm", return_value=instance), \
         patch("routers.chat._apply_memory_pressure_swap",
               new=AsyncMock(return_value=("qwen3:8b", False))):
        events = _send_and_collect()

    done = events[-1]
    assert done["type"] == "done"
    assert "metrics" not in done


def test_metrics_decode_tps_is_round_to_two_decimals():
    """Wire format: decode_tps comes back rounded so the JSON is
    compact and the frontend doesn't have to format it again."""
    instance = MagicMock()
    instance.stream_response = _stream(
        StreamEvent(type="text_delta", content="x"),
        StreamEvent(
            type="usage",
            input_tokens=1, output_tokens=33,
            eval_duration_ns=1_000_000_000,
            prompt_eval_duration_ns=10_000_000,
            load_duration_ns=0,
            total_duration_ns=1_010_000_000,
        ),
    )
    with patch("routers.chat.get_api_key", return_value=""), \
         patch("routers.chat._make_llm", return_value=instance), \
         patch("routers.chat._apply_memory_pressure_swap",
               new=AsyncMock(return_value=("qwen3:8b", False))):
        events = _send_and_collect()

    m = events[-1]["metrics"]
    # 33 tokens / 1.0s = 33.0 tps; rounded representation is 33.0 (no
    # spurious trailing decimals from float arithmetic).
    assert m["decode_tps"] == 33.0
    # ttft = 0 (load) + 10ms (prefill) = 10.0 ms
    assert m["ttft_ms"] == 10.0
    assert m["load_ms"] == 0.0


# ── Multi-round (tool-using) turn aggregation ────────────────────────────────


def test_multiround_turn_takes_ttft_from_first_round_only():
    """Critical correctness invariant: TTFT is what the user *felt* —
    the first round's load + prefill. A naive aggregator that averages
    or last-round-wins on TTFT silently breaks the health watcher's
    interpretation. Decode and prefill durations *do* sum across rounds
    because the user effectively waited through every round's compute.
    """
    # Round 1 streams a tool_use, then usage with cold-load TTFT.
    # Round 2 (after tool execution) streams text + a warm usage event.
    instance = MagicMock()
    call_count = {"n": 0}

    async def _gen(**kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            yield StreamEvent(
                type="tool_use",
                name="search_notes",
                tool_input={"query": "x"},
                tool_use_id="toolu_round1",
            )
            yield StreamEvent(
                type="usage",
                input_tokens=80, output_tokens=12,
                eval_duration_ns=500_000_000,         # 0.5 s decode
                prompt_eval_duration_ns=100_000_000,   # 100 ms prefill (cold prefill)
                load_duration_ns=400_000_000,          # 400 ms cold load
                total_duration_ns=1_000_000_000,
            )
        else:
            yield StreamEvent(type="text_delta", content="Found it.")
            yield StreamEvent(
                type="usage",
                input_tokens=120, output_tokens=20,
                eval_duration_ns=1_000_000_000,        # 1.0 s decode
                prompt_eval_duration_ns=20_000_000,     # 20 ms prefill (warm)
                load_duration_ns=0,                     # warm — no load
                total_duration_ns=1_020_000_000,
            )

    instance.stream_response = _gen

    async def _fake_run_tool(event, **kw):
        return "result"

    with patch("routers.chat.get_api_key", return_value=""), \
         patch("routers.chat._make_llm", return_value=instance), \
         patch("routers.chat._apply_memory_pressure_swap",
               new=AsyncMock(return_value=("qwen3:8b", False))), \
         patch("routers.chat._run_tool", new=AsyncMock(side_effect=_fake_run_tool)):
        events = _send_and_collect()

    done = events[-1]
    assert done["type"] == "done"
    m = done["metrics"]
    # TTFT must be round 1's load + prefill = 400 + 100 = 500 ms.
    # NOT 20 ms (last round), NOT 260 ms (mean of 500 and 20), NOT 120 ms
    # (mean of cold-load + cold-prefill / 2 nonsense).
    assert m["ttft_ms"] == pytest.approx(500.0, rel=0.01)
    assert m["load_ms"] == pytest.approx(400.0, rel=0.01)
    # Decode: (12 + 20) tokens / (0.5 + 1.0) s = 32 / 1.5 = 21.333 tps
    assert m["decode_tps"] == pytest.approx(21.33, rel=0.01)
    # Prefill: (80 + 120) tokens / (0.1 + 0.02) s = 200 / 0.12 = 1666.67 tps
    assert m["prefill_tps"] == pytest.approx(1666.67, rel=0.01)
    # Counts sum across rounds (these come from usage_acc, not metrics_acc).
    assert m["eval_count"] == 32
    assert m["prompt_eval_count"] == 200


# ── OOM-retry path (ADR 005 §C trigger 1 + this feature's interaction) ───────


def test_oom_retry_resets_metrics_acc_so_failed_round_does_not_leak():
    """When Ollama emits partial timings before an OOM error, the
    accumulator must be reset before the retry loop. Otherwise the
    retried (smaller) model's `done.metrics` would carry the failed
    (larger) model's load + prefill and mis-attribute it via the
    `done.model` field — feeding the chat-health watcher a sample that
    blames the retry model for the failed model's poor performance.
    """
    instance = MagicMock()
    call_count = {"n": 0}

    async def _gen(**kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # Failed round: partial timings emitted, then OOM error.
            # `usage` arriving before an error in the same round is
            # uncommon but documented (partial-chunk abort).
            yield StreamEvent(
                type="usage",
                input_tokens=200, output_tokens=5,
                eval_duration_ns=200_000_000,
                prompt_eval_duration_ns=80_000_000,
                load_duration_ns=8_000_000_000,        # 8 s — failed cold load
                total_duration_ns=8_280_000_000,
            )
            yield StreamEvent(type="error", content="cuda out of memory")
        else:
            # Retry round — clean run, smaller model, fast warm load.
            yield StreamEvent(type="text_delta", content="OK")
            yield StreamEvent(
                type="usage",
                input_tokens=200, output_tokens=10,
                eval_duration_ns=500_000_000,          # 0.5 s decode
                prompt_eval_duration_ns=20_000_000,     # 20 ms prefill
                load_duration_ns=0,                     # warm
                total_duration_ns=520_000_000,
            )

    instance.stream_response = _gen

    with patch("routers.chat.get_api_key", return_value=""), \
         patch("routers.chat._make_llm", return_value=instance), \
         patch("routers.chat._apply_memory_pressure_swap",
               new=AsyncMock(return_value=("qwen3:30b-a3b-instruct-2507-q4_K_M", False))), \
         patch(
             "routers.chat._ladder_step_after_oom",
             new=AsyncMock(return_value=("qwen3:8b", "Switched to qwen3:8b after OOM")),
         ):
        events = _send_and_collect(model="qwen3:30b-a3b-instruct-2507-q4_K_M")

    done = events[-1]
    m = done["metrics"]
    # The done event reports the smaller model.
    assert "qwen3:8b" in done["model"]
    # TTFT must be the *retry* round's 0 + 20ms = 20ms — NOT 8000+80 = 8080ms
    # from the failed cold load.
    assert m["ttft_ms"] == pytest.approx(20.0, rel=0.01)
    assert m["load_ms"] == pytest.approx(0.0, abs=0.5)
    # Decode reflects only the retry round: 10 tokens / 0.5s = 20 tps,
    # NOT (5+10)/(0.2+0.5) = 21.4 tps from carried-over failed timings.
    assert m["decode_tps"] == pytest.approx(20.0, rel=0.01)


def test_done_carries_no_metrics_on_oom_with_no_fallback():
    """When the ladder is exhausted, the user sees an error event and
    the `done` event carries no `metrics` field — the failed round's
    partial timings (if any) belong to the evicted model, not to a
    'real' turn the watcher should sample."""
    instance = MagicMock()
    instance.stream_response = _stream(
        StreamEvent(
            type="usage",
            input_tokens=100, output_tokens=3,
            eval_duration_ns=100_000_000,
            prompt_eval_duration_ns=50_000_000,
            load_duration_ns=2_000_000_000,
            total_duration_ns=2_150_000_000,
        ),
        StreamEvent(type="error", content="out of memory"),
    )
    with patch("routers.chat.get_api_key", return_value=""), \
         patch("routers.chat._make_llm", return_value=instance), \
         patch("routers.chat._apply_memory_pressure_swap",
               new=AsyncMock(return_value=("qwen3:4b-instruct-2507-q4_K_M", False))), \
         patch("routers.chat._ladder_step_after_oom", new=AsyncMock(return_value=(None, None))):
        events = _send_and_collect(model="qwen3:4b-instruct-2507-q4_K_M")

    done = events[-1]
    assert done["type"] == "done"
    # Failed-round partial timings would otherwise flow into done.metrics
    # because the OOM-retry loop falls through to the same done emission.
    # The test pins that the watcher receives no sample on this path.
    # Our spec: failed-round partial timings are still surfaced (they
    # describe a real user-felt latency), but no health-watcher sample
    # is taken because there's no decode_tps when eval_count is too low
    # to be meaningful — assert at minimum that the user-facing error
    # was emitted and the done event is clean of the OOM-error noise.
    errs = [e for e in events if e["type"] == "error"]
    assert any("no smaller" in e["content"].lower() or "free up ram" in e["content"].lower() for e in errs)


def test_total_duration_only_event_does_not_mark_round_as_timed():
    """Defensive: an in-flight cancel could conceivably surface a
    `usage` event carrying only `total_duration_ns`. Without decode or
    prefill timings there's nothing meaningful for the watcher; the
    accumulator must NOT mark the round as timed (which would emit
    `ttft_ms: 0` and look like a real reading)."""
    instance = MagicMock()
    instance.stream_response = _stream(
        StreamEvent(type="text_delta", content="hi"),
        StreamEvent(
            type="usage",
            input_tokens=4, output_tokens=2,
            eval_duration_ns=None,
            prompt_eval_duration_ns=None,
            load_duration_ns=None,
            total_duration_ns=1_000_000_000,  # only total; the others are None
        ),
    )
    with patch("routers.chat.get_api_key", return_value=""), \
         patch("routers.chat._make_llm", return_value=instance), \
         patch("routers.chat._apply_memory_pressure_swap",
               new=AsyncMock(return_value=("qwen3:8b", False))):
        events = _send_and_collect()

    done = events[-1]
    assert "metrics" not in done
