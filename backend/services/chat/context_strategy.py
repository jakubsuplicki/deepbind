"""Context-assembly strategy abstraction (ADR 010).

A ContextStrategy decides which messages from the running session history
are assembled into the model's context window for each chat turn. The
abstraction is the unit of swap for the conversation-replay eval harness
and the slot through which any future v1.1+ compaction policy attaches.

Production today runs FullHistoryStrategy — a literal identity over the
input — so the abstraction lands behavior-preserving. Eval-side strategies
(naive recent-N truncation, retrieval-substitution per ADR 009) and any
future per-profile compaction policy slot in here without touching the
chat pipeline.

Why an abstraction at all when only the identity strategy ships today:
ADR 010's gate decision compares the production strategy against
alternatives, and that comparison cannot run if the production path has
no swap point. The Protocol is the smallest possible swap point.
"""

from typing import Protocol, runtime_checkable


@runtime_checkable
class ContextStrategy(Protocol):
    """Decides which messages go into the model's context window.

    Implementations are expected to be stateless across calls and pure with
    respect to the input messages list — return a new list, do not mutate.
    The runner pins models, seeds, and temperature; strategy implementations
    must not introduce additional non-determinism.

    The signature today is intentionally minimal — input messages only,
    output messages only. Strategies that need additional context (token
    budget, retrieval engine, session id) take it via constructor parameters.
    The signature may extend to a context object when an eval-side strategy
    actually needs one; until then YAGNI.

    On Python 3.12+ (the project's supported floor), ``@runtime_checkable``
    enforces presence of both the ``name`` attribute and the ``assemble``
    method at ``isinstance`` time. A class missing either fails the check.
    Pinned by ``test_runtime_checkable_enforces_name_attribute``; if that
    test ever flips, the eval runner needs an explicit ``name`` validator.
    """

    name: str

    def assemble(self, messages: list[dict]) -> list[dict]:
        """Return the message list to send to the model.

        ``messages`` is the full session history as returned by
        ``session_service.get_messages``. The returned list must be
        well-formed for the configured LLM provider (matching role
        sequencing, intact tool_use / tool_result pairs). The result must
        be a ``list``; ``None`` and other types are rejected at the chat
        router boundary.
        """
        ...


class FullHistoryStrategy:
    """Pass the entire session history through unchanged.

    Reproduces the pre-ADR-010 chat-pipeline behavior. The returned list is a
    shallow copy of the input (the message dicts themselves are shared
    references); this is identical to what ``session_service.get_messages``
    already does, so the production behavior is preserved end-to-end. Note
    "behavior-preserving" rather than "object-identical" — there is one
    extra list-allocation per turn versus the pre-refactor path, which is
    immaterial for any realistic conversation length.
    """

    name = "full-history"

    def assemble(self, messages: list[dict]) -> list[dict]:
        return list(messages)


# The default singleton. Imported by the chat router; eval-side runners
# pass their own strategy instance through dependency injection.
DEFAULT_STRATEGY: ContextStrategy = FullHistoryStrategy()
