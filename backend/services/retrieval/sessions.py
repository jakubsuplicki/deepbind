"""Session-scoped retrieval entry point for ADR 009 compaction.

When the conversation overflows the recent window, the production
compaction strategy substitutes dropped turns by retrieving from the
**markdown vault**, not from the dropped portion of the live history
(which is what the eval-side ``RetrievalSubstitutionV1Strategy`` does).

The vault contains every conversation the user has had, persisted by
``session_service.save_session_to_memory`` to ``memory/conversations/``
as a Markdown note with frontmatter. Each note carries the original
``session_id`` in its frontmatter — that is the hook used to exclude
**this** conversation from its own retrieval substitution. Anything
older from the same session that was already saved (the previous
auto-save's snapshot) is excluded so the strategy doesn't show the user
a stale prefix of the conversation they're literally in.

This module reuses ``memory_service.list_notes`` for the BM25 score and
reads each top result's body off disk. We deliberately don't run the
full hybrid retrieval pipeline (BM25 + cosine + graph) here:

- BM25 alone is enough for "find a conversation that mentioned X" —
  semantic similarity matters less when the query and the corpus share
  nearly identical surface text (the corpus *is* user conversations).
- Skipping cosine/graph keeps compaction latency low. Compaction fires
  on the per-turn boundary; adding hundreds of milliseconds per long
  turn would defeat the eval-validated quality win.
- The retrieval pipeline's enrichment / Jira / facet machinery is
  irrelevant to conversation-history retrieval and would only add
  surface area to the test matrix.

If quality measurement (open follow-up #2 in ADR 009) shows BM25 alone
is insufficient, this entry point is the right place to graduate to the
full pipeline — the function signature stays stable.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

from services import memory_service

logger = logging.getLogger(__name__)


# Body-snippet cap for each retrieved conversation. We don't want a
# 50KB conversation note to dominate the kept-window budget — the
# compaction caller already enforces a top_k limit, but a single huge
# conversation can still blow the substitution block past usefulness.
# 1500 chars (~375 tokens) is enough for the most-relevant section
# without paying the full transcript cost.
_SNIPPET_CHAR_BUDGET = 1500
# Hard ceiling on how many candidates we ask the index for. We over-
# fetch ``top_k * 2`` to absorb the same-session filter, but a caller
# passing a very large ``top_k`` would otherwise expose the FTS index
# to an unbounded query. 50 is well above any realistic ``top_k``
# (the compaction default is 3) and well below pathological values.
_LIST_NOTES_CEILING = 50


async def find_earlier_turn_context(
    query: str,
    *,
    current_session_id: str = "",
    top_k: int = 3,
    workspace_path: Optional[Path] = None,
) -> List[dict]:
    """Return up to ``top_k`` conversation-vault matches for ``query``.

    Used by ``compaction_service.compact_messages`` when the recent
    window overflows the configured budget. Each result is a dict:

      {
        "path":     str — vault-relative path (conversations/...md)
        "title":    str — note title from frontmatter
        "snippet":  str — body excerpt up to _SNIPPET_CHAR_BUDGET chars
        "session_id": Optional[str] — original session id from frontmatter,
                     when present; not all conversation notes carry it
                     (legacy notes saved before the session_id field landed
                     in save_session_to_memory).
      }

    Excludes the **current** session's own saved conversation note when
    ``current_session_id`` is provided. Without this exclusion the user
    would see the start of the very conversation they're in re-injected
    as "earlier context."

    Returns ``[]`` for empty query, no vault, or no matches. Never
    raises — compaction must continue even if retrieval fails.
    """
    if not query or not query.strip():
        return []
    if top_k <= 0:
        return []

    try:
        candidates = await memory_service.list_notes(
            folder="conversations",
            search=query,
            # Over-fetch so the same-session filter doesn't shrink the
            # result set below ``top_k``, but cap so a misconfigured
            # caller can't hammer the index.
            limit=min(top_k * 2, _LIST_NOTES_CEILING),
            workspace_path=workspace_path,
        )
    except Exception as exc:  # noqa: BLE001 — never break compaction on retrieval failure
        logger.warning("find_earlier_turn_context: list_notes failed: %s", exc)
        return []

    if not candidates:
        return []

    out: List[dict] = []
    for cand in candidates:
        if len(out) >= top_k:
            break
        path = cand.get("path", "")
        if not path:
            continue
        try:
            note = await memory_service.get_note(path, workspace_path=workspace_path)
        except Exception:
            # A candidate that exists in the index but not on disk shouldn't
            # take down the whole turn. Skip and keep going.
            continue
        fm = note.get("frontmatter") or {}
        note_session_id = str(fm.get("session_id") or "")
        if current_session_id and note_session_id == current_session_id:
            # Don't substitute "earlier context" with the same conversation
            # the user is actively having.
            continue
        body = note.get("content") or ""
        # Strip frontmatter from the body for the snippet — get_note returns
        # the full file content; we want only the prose so the substituted
        # block reads cleanly.
        body = _strip_frontmatter(body)
        snippet = body[:_SNIPPET_CHAR_BUDGET].strip()
        if not snippet:
            continue
        out.append(
            {
                "path": path,
                "title": str(fm.get("title") or note.get("title") or path),
                "snippet": snippet,
                "session_id": note_session_id or None,
            }
        )
    return out


def _strip_frontmatter(content: str) -> str:
    """Drop a leading YAML frontmatter block (--- ... ---) from a note body.

    A simple textual strip is sufficient — every conversation note we save
    uses the canonical ``add_frontmatter`` path so the delimiter shape is
    fixed. If the shape ever drifts, the snippet would just include some
    YAML at the top, which is degraded but not broken.
    """
    if not content.startswith("---"):
        return content
    # Find the closing fence.
    closing = content.find("\n---", 3)
    if closing == -1:
        return content
    # Skip past the closing fence and the newline immediately after.
    rest = content[closing + len("\n---"):]
    if rest.startswith("\n"):
        rest = rest[1:]
    return rest
