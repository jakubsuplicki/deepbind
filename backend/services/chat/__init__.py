"""Chat-pipeline support services.

Currently exposes the ContextStrategy abstraction (ADR 010): the unit of
swap that decides which messages from session history are assembled into
the model's context window. The default production strategy is
FullHistoryStrategy, which is a behavior-preserving identity over the
existing pre-ADR-010 chat path.

Why a sub-package instead of a flat ``services/chat_strategy.py`` next to
``services/claude.py``: ADR 010 anticipates additional chat-pipeline
support modules (judge providers for the eval harness, alternative
strategy implementations, the eval-side replay runner's chat-side glue).
A sub-package keeps the related concerns together and the import surface
stable as those modules land. Existing flat modules (``claude.py``,
``llm_service.py``) stay where they are.
"""

from .context_strategy import (
    ContextStrategy,
    DEFAULT_STRATEGY,
    FullHistoryStrategy,
)

__all__ = [
    "ContextStrategy",
    "DEFAULT_STRATEGY",
    "FullHistoryStrategy",
]
