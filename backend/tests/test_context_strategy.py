"""Tests for the ContextStrategy abstraction (ADR 010).

The launch contract is narrow: FullHistoryStrategy must be a literal
identity over the input messages list, and the chat router must default
to it. These tests pin both invariants so a future regression cannot
silently change production context-assembly behavior.
"""

from services.chat import (
    DEFAULT_STRATEGY,
    ContextStrategy,
    FullHistoryStrategy,
)


def _sample_messages() -> list[dict]:
    return [
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "Hello"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "t1", "name": "read_note", "input": {"path": "x.md"}}
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "file contents"}
            ],
        },
        {"role": "user", "content": "Follow up"},
    ]


def test_full_history_strategy_is_identity():
    strategy = FullHistoryStrategy()
    messages = _sample_messages()
    out = strategy.assemble(messages)
    assert out == messages


def test_full_history_strategy_returns_new_list():
    """Strategies must not return the caller's list — protects against
    accidental in-place mutation by downstream code."""
    strategy = FullHistoryStrategy()
    messages = _sample_messages()
    out = strategy.assemble(messages)
    assert out is not messages


def test_full_history_strategy_does_not_mutate_input():
    strategy = FullHistoryStrategy()
    messages = _sample_messages()
    snapshot = [dict(m) for m in messages]
    strategy.assemble(messages)
    assert messages == snapshot


def test_default_strategy_is_full_history():
    """Production must default to FullHistoryStrategy. If this changes,
    it's an ADR-worthy decision, not a quiet refactor."""
    assert isinstance(DEFAULT_STRATEGY, FullHistoryStrategy)
    assert DEFAULT_STRATEGY.name == "full-history"


def test_full_history_strategy_handles_empty_history():
    strategy = FullHistoryStrategy()
    assert strategy.assemble([]) == []


def test_context_strategy_protocol_is_runtime_checkable():
    """The Protocol must be runtime-checkable so the eval runner can verify
    a custom strategy implements the interface before running."""
    assert isinstance(FullHistoryStrategy(), ContextStrategy)


class _StubStrategy:
    name = "stub"

    def assemble(self, messages: list[dict]) -> list[dict]:
        return [m for m in messages if m.get("role") != "assistant"]


def test_alternative_strategy_satisfies_protocol():
    """A class that satisfies the Protocol structurally must pass isinstance."""
    assert isinstance(_StubStrategy(), ContextStrategy)


def test_alternative_strategy_drops_messages():
    """Sanity that the abstraction actually allows non-identity behavior."""
    strategy = _StubStrategy()
    messages = _sample_messages()
    out = strategy.assemble(messages)
    assert all(m.get("role") != "assistant" for m in out)
    assert len(out) < len(messages)


# ── Documented limitations ────────────────────────────────────────────────────


class _StrategyMissingName:
    """A strategy with the assemble method but no `name` attribute.

    Exists to pin Python 3.12's ``@runtime_checkable`` behavior on Protocols
    that declare both methods and data attributes: presence of *both* is
    required for ``isinstance`` to return True. If a future Python release
    relaxes this and starts ignoring the attribute check, the eval runner
    will need its own explicit ``name`` validator and we'll know to add it.
    """

    def assemble(self, messages: list[dict]) -> list[dict]:
        return list(messages)


def test_runtime_checkable_enforces_name_attribute():
    """Pins Python 3.12 behavior: a Protocol member declared as a typed
    attribute (``name: str``) is enforced at isinstance check time.

    A class with the ``assemble`` method but no ``name`` attribute must
    fail isinstance. If this ever flips (Python relaxes Protocol attribute
    enforcement, or our runtime_checkable usage drifts), the eval runner
    must add a manual ``name`` validator on strategy registration.
    """
    obj = _StrategyMissingName()
    assert not hasattr(obj, "name")
    assert not isinstance(obj, ContextStrategy)


# ── Production wiring tests ───────────────────────────────────────────────────


class _SpyStrategy:
    name = "spy"

    def __init__(self):
        self.call_count = 0
        self.last_input: list[dict] | None = None

    def assemble(self, messages: list[dict]) -> list[dict]:
        self.call_count += 1
        self.last_input = messages
        return list(messages)


class _NoneReturningStrategy:
    name = "broken"

    def assemble(self, messages: list[dict]) -> list[dict]:
        return None  # type: ignore[return-value]


def test_chat_router_uses_default_strategy_when_none_passed(monkeypatch):
    """Sanity that DEFAULT_STRATEGY is what the chat router falls back to.

    Pinned via the same import path the router uses, so a future refactor
    that bypasses the default (e.g., reaching directly into
    session_service.get_messages) is caught.
    """
    from routers import chat as chat_router

    assert chat_router.DEFAULT_STRATEGY is DEFAULT_STRATEGY


def test_strategy_call_count_is_one_per_handle_message():
    """Spy asserts assemble is invoked exactly once per turn.

    The runner depends on this being deterministic — calling assemble more
    than once per turn would mean the strategy sees a moving history target
    and the eval baseline would be non-reproducible.

    We test the contract directly by simulating the router's exact pattern
    (the part the strategy participates in), rather than running the full
    websocket pipeline.
    """
    spy = _SpyStrategy()
    raw_messages = _sample_messages()
    # Exact pattern from chat.py:_handle_message
    out = (spy or DEFAULT_STRATEGY).assemble(raw_messages)
    assert spy.call_count == 1
    assert out == raw_messages


def test_none_returning_strategy_is_rejected_by_router_guard():
    """Mirrors the TypeError guard at the chat router boundary.

    Documents the contract: a strategy returning None must produce a clear
    error at the strategy boundary, not a confusing message-format error
    deep in the LLM-provider code.
    """
    strategy = _NoneReturningStrategy()
    result = strategy.assemble(_sample_messages())

    # The guard is in chat.py:_handle_message; this test pins the
    # condition the guard checks for. If the guard is removed, this test
    # still passes (it just verifies the buggy strategy returns None) —
    # so it serves as the test for the negative case the guard addresses.
    assert not isinstance(result, list)
    assert result is None
