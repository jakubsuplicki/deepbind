"""Eval-side ContextStrategy implementations (ADR 010).

These strategies are eval-only — they live under ``backend/tests/`` rather
than ``backend/services/`` because they exist to compete against the
production default (``FullHistoryStrategy``) in the conversation-replay
gate, not to ship in the user-facing chat path. If one of them later
graduates to production, it moves to ``services/chat/`` at that time.

Currently exposes:

- :class:`NaiveTruncateStrategy` — keep only the last ``recent_n`` user
  turns. The cheapest possible compaction baseline; the strategy that
  retrieval-substitution must beat to justify its complexity.

- :class:`RetrievalSubstitutionV1Strategy` — ADR 009's retrieval-first
  stance. Drops older turns identically to naive-truncate, but before
  returning the kept window, retrieves the top-K most-relevant *dropped*
  turn pairs by keyword overlap with the latest user turn and re-injects
  them as a synthesized prefix. The hypothesis: a small amount of
  targeted retrieval over the dropped context closes the gap between
  naive truncation and full-history at a fraction of the token cost.
  This is the strategy the gate has to evaluate against naive truncation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class NaiveTruncateStrategy:
    """Keep only the last ``recent_n`` user turns plus everything after.

    Counts user *turns* — not messages — because tool_use / tool_result
    pairs are interleaved as assistant + tool-role-user messages between
    user turns. Truncating mid-pair would break the Anthropic protocol
    (tool_use block without matching tool_result is rejected by the
    provider). Cutting at a user-turn boundary preserves all tool pairs
    that fall within the kept suffix, since pairs are always contained
    between consecutive user turns.

    The "user turn" definition explicitly excludes user messages whose
    content is a tool_result wrapper — those are protocol-mandated
    responses to a preceding tool_use, not actual user inputs. Without
    this distinction a long tool_loop history would falsely inflate the
    user-turn count.

    Per ADR 010 the eval sweeps ``recent_n`` over a small range (4, 8,
    12, 16) so the cheapest viable baseline is found, not just the first
    value tested.
    """

    recent_n: int

    def __post_init__(self) -> None:
        if self.recent_n <= 0:
            raise ValueError(f"recent_n must be > 0, got {self.recent_n}")
        # Set the public ``name`` attribute that the ContextStrategy
        # Protocol declares; embed N so different sweeps produce
        # distinguishable baseline filenames.
        self.name = f"naive-truncate-{self.recent_n}"

    def assemble(self, messages: list[dict]) -> list[dict]:
        user_turn_indices = [
            i for i, m in enumerate(messages) if _is_real_user_turn(m)
        ]

        if len(user_turn_indices) <= self.recent_n:
            return list(messages)

        # Keep from the (recent_n)-th-to-last user turn onward. Everything
        # before that index is dropped, including any leading scripted
        # assistant turns or tool pairs that belong to dropped user turns.
        cut_index = user_turn_indices[-self.recent_n]
        return list(messages[cut_index:])


# ── Retrieval-substitution v1 ─────────────────────────────────────────────────


@dataclass
class RetrievalSubstitutionV1Strategy:
    """ADR 009's retrieval-first compaction strategy, eval-side v1.

    Behavior:

    1. Truncate to the last ``recent_n`` user turns plus everything after
       (identical to :class:`NaiveTruncateStrategy`).
    2. Identify the most recent user-turn text in the kept window — this
       is the "query" for retrieval.
    3. Score each *dropped* real-user-turn message against the query by
       lower-cased word overlap, ignoring stop words and tokens shorter
       than ``min_token_len``. The score for a dropped turn is the count
       of distinct content tokens it shares with the query.
    4. Take the top ``top_k`` dropped turns whose score is at least
       ``min_overlap``. Pair each with the assistant message that
       immediately follows it in the original history (if any), since a
       Q+A pair is more informative than the question alone.
    5. Synthesize one ``user``-role message that summarizes the retrieved
       pairs and prepend it to the kept window. Using the user role
       (rather than ``system``) keeps the prefix inside the conversation
       payload regardless of how the chat callable handles the system
       prompt — every adapter accepts a leading user message.

    Why this shape rather than vault retrieval:

    The conversation-replay fixtures don't have a vault populated with
    the buried details — the history *is* the corpus. Production's
    retrieval pipeline reaches into the workspace; the eval-side v1
    reaches into the dropped portion of the conversation. Both test the
    same hypothesis (targeted retrieval can substitute for full-history)
    against the substrate available. The production strategy with vault
    retrieval is the next iteration; this one isolates the retrieval-
    substitution variable from the workspace-population variable.

    Determinism:

    Token overlap is computed against a fixed stop-word list embedded
    here (no external dependencies, no surprise model). Ties in score
    are broken by recency (most recent dropped turn wins), so two runs
    over identical history produce identical retrieval picks.
    """

    recent_n: int = 8
    top_k: int = 3
    min_overlap: int = 1
    min_token_len: int = 3

    def __post_init__(self) -> None:
        if self.recent_n <= 0:
            raise ValueError(f"recent_n must be > 0, got {self.recent_n}")
        if self.top_k <= 0:
            raise ValueError(f"top_k must be > 0, got {self.top_k}")
        if self.min_overlap < 0:
            raise ValueError(f"min_overlap must be >= 0, got {self.min_overlap}")
        # Public ``name`` for the ContextStrategy Protocol; encodes both
        # window and retrieval depth so an N-K sweep produces distinct
        # baseline filenames.
        self.name = f"retrieval-substitution-v1-n{self.recent_n}-k{self.top_k}"

    def assemble(self, messages: list[dict]) -> list[dict]:
        user_turn_indices = [
            i for i, m in enumerate(messages) if _is_real_user_turn(m)
        ]
        if len(user_turn_indices) <= self.recent_n:
            # Nothing to drop → nothing to retrieve. Identical to full-history.
            return list(messages)

        cut_index = user_turn_indices[-self.recent_n]
        dropped = messages[:cut_index]
        kept = list(messages[cut_index:])

        query_tokens = self._extract_query_tokens(kept)
        if not query_tokens:
            # No query → no meaningful retrieval. Behave like naive-truncate.
            return kept

        scored_pairs = self._score_dropped_pairs(
            messages, dropped_count=cut_index, query_tokens=query_tokens
        )
        if not scored_pairs:
            return kept

        # Take the top_k by score; ties broken by recency (later index wins).
        # Sort ascending by (-score, index_desc) so that highest score and most
        # recent come first; we then re-sort the chosen pairs by index ascending
        # so the synthesized block reads in chronological order.
        scored_pairs.sort(key=lambda p: (-p["score"], -p["user_index"]))
        chosen = scored_pairs[: self.top_k]
        if not chosen:
            return kept
        chosen.sort(key=lambda p: p["user_index"])

        synth = self._synthesize_retrieval_block(chosen)
        return [synth] + kept

    # ── Internals ────────────────────────────────────────────────────────────

    def _extract_query_tokens(self, kept: list[dict]) -> set[str]:
        """Tokens from the last user turn in the kept window. Empty when
        no real user turn exists yet (first-message case)."""
        for msg in reversed(kept):
            if _is_real_user_turn(msg):
                return _tokenize_for_retrieval(
                    _flatten_text(msg.get("content")),
                    min_len=self.min_token_len,
                )
        return set()

    def _score_dropped_pairs(
        self,
        messages: list[dict],
        *,
        dropped_count: int,
        query_tokens: set[str],
    ) -> list[dict]:
        """For each real user turn in messages[:dropped_count], score it
        against ``query_tokens`` and pair it with the immediately-following
        assistant message (if any).

        Returns a list of dicts: ``{user_index, user_text, assistant_text,
        score}``. Only includes pairs whose score is at least
        ``min_overlap``.
        """
        out: list[dict] = []
        for i in range(dropped_count):
            msg = messages[i]
            if not _is_real_user_turn(msg):
                continue
            user_text = _flatten_text(msg.get("content"))
            tokens = _tokenize_for_retrieval(user_text, min_len=self.min_token_len)
            score = len(tokens & query_tokens)
            if score < self.min_overlap:
                continue
            assistant_text = ""
            # Look forward up to a few messages for the next assistant text;
            # tool turns may be interleaved in tool-loop fixtures.
            for j in range(i + 1, min(i + 4, dropped_count)):
                follow = messages[j]
                if follow.get("role") in ("assistant", "assistant_scripted"):
                    assistant_text = _flatten_text(follow.get("content"))
                    break
            out.append(
                {
                    "user_index": i,
                    "user_text": user_text,
                    "assistant_text": assistant_text,
                    "score": score,
                }
            )
        return out

    def _synthesize_retrieval_block(self, chosen: list[dict]) -> dict:
        """Build a leading user message summarizing the retrieved pairs.

        Uses a user-role message instead of system so the strategy's
        output is self-contained — the runner already prepends a system
        prompt and we don't want to fight over that slot.
        """
        lines = [
            "[Retrieved earlier-conversation context, dropped from the visible window:]"
        ]
        for pair in chosen:
            lines.append("")
            lines.append(f"You earlier said: \"{pair['user_text'].strip()}\"")
            if pair["assistant_text"]:
                lines.append(
                    f"I responded: \"{pair['assistant_text'].strip()}\""
                )
        lines.append("")
        lines.append(
            "[End retrieved context. The visible conversation continues below.]"
        )
        return {"role": "user", "content": "\n".join(lines)}


# ── Internal helpers ─────────────────────────────────────────────────────────


# A small embedded stop-word list — keeping retrieval deterministic without
# pulling in NLTK / sklearn just for token filtering. Shaped for English (the
# primary fixture language) with a few obvious Polish stop-words too, since
# fixture #5 and #15 are bilingual. The list does not need to be exhaustive;
# false-positive matches only hurt precision a little.
_RETRIEVAL_STOP_WORDS: frozenset[str] = frozenset(
    {
        # English determiners / conjunctions / aux verbs
        "the", "and", "for", "but", "with", "this", "that", "from", "have",
        "has", "had", "you", "are", "was", "were", "will", "would", "could",
        "should", "can", "any", "all", "some", "what", "which", "who", "how",
        "why", "when", "where", "there", "here", "their", "they", "them",
        "your", "yours", "mine", "ours", "ours", "his", "hers", "its", "our",
        "about", "into", "onto", "over", "under", "out", "off", "than", "then",
        "just", "very", "too", "also", "only", "more", "most", "less", "least",
        "one", "two", "three", "many", "few", "lot", "lots", "kind", "sort",
        "yes", "yeah", "yep", "nope", "okay", "right", "well", "really", "quite",
        "good", "bad", "new", "old", "best", "worst", "fine", "nice", "great",
        "want", "need", "going", "doing", "being", "make", "made", "make",
        "give", "gave", "take", "took", "get", "got", "see", "saw", "say",
        "said", "tell", "told", "ask", "asked", "use", "used", "try", "tried",
        "let", "let's", "lets", "thanks", "thank", "actually", "anyway",
        "anyway", "anyway",
        # Polish small stop-words (fixture-bilingual hygiene)
        "jest", "tak", "nie", "moim", "moja", "moje", "mój", "moich",
        "tego", "tej", "ten", "tych", "albo", "lub", "albo", "też", "byc",
        "być", "miał", "była", "było", "ale", "czy", "jak", "ze", "że",
        "dla", "tym", "tym",
        # tokens we'd otherwise score on heavily
        "thing", "things", "stuff", "case", "cases", "example", "examples",
    }
)


_TOKEN_RE = re.compile(r"[A-Za-z0-9À-ɏ]+")
"""Tokenizer regex. Matches ASCII alphanumerics plus Latin Extended ranges
(covers Polish diacritics like ą/ć/ę/ł/ń/ó/ś/ź/ż). Anything else is treated
as a separator."""


def _tokenize_for_retrieval(text: str, *, min_len: int) -> set[str]:
    """Lower-case tokens of length ≥ ``min_len`` minus stop-words.

    Returns a set so token overlap is computed by intersection size, which
    rewards diverse content tokens rather than repetitive ones (a query
    repeating "deadline" five times scores no higher than a query mentioning
    it once).
    """
    if not text:
        return set()
    return {
        t.lower()
        for t in _TOKEN_RE.findall(text)
        if len(t) >= min_len and t.lower() not in _RETRIEVAL_STOP_WORDS
    }


def _flatten_text(content) -> str:
    """Render a message's ``content`` (string OR Anthropic-style block list)
    into a flat string for tokenization. Tool blocks are skipped."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
        return " ".join(parts)
    return ""


def _is_real_user_turn(msg: dict) -> bool:
    """A user message that represents an actual user input.

    Returns False for user messages whose entire content is a tool_result
    block (protocol-mandated tool response — not a real turn). Returns
    True for plain string content and for content lists that include
    text blocks.
    """
    if msg.get("role") != "user":
        return False
    content = msg.get("content")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        # If every block is a tool_result, this is a tool-response message,
        # not a user turn. Mixed content (rare in practice) counts as a
        # user turn — the user said something AND attached a tool result.
        if not content:
            return True
        all_tool_results = all(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        )
        return not all_tool_results
    return True
