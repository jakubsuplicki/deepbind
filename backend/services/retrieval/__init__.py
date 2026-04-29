"""Jira-aware hybrid retrieval package (step 22f).

Re-exports the public ``retrieve()`` function so existing imports
(``from services.retrieval import retrieve``) keep working.

Also exposes the ADR 009 session-scoped entry point
``find_earlier_turn_context`` used by the production compaction
service to substitute dropped conversation turns from the markdown
vault.
"""

from services.retrieval.pipeline import retrieve, retrieve_with_intent  # noqa: F401
from services.retrieval.sessions import find_earlier_turn_context  # noqa: F401
