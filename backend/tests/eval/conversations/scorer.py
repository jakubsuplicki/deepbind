"""Mechanical scoring for conversation-replay eval (ADR 010).

Scores an assistant_target turn's response against the fixture's
``expected_facts`` and ``must_not_contain`` lists. Pure functions, no I/O,
no LLM calls — this is the load-bearing signal that ADR 010's decision
gate runs against. The judge protocol (when needed) is layered on top.

Severity taxonomy (added in the higher-rigor pass):

- ``CLEAN_PASS`` — every fact passes, no guard triggers.
- ``PARTIAL`` — at least one fact passes but not all, no guard triggers.
  The model knew *something* but not everything; still useful signal.
- ``NO_ANSWER`` — zero facts pass, no guard triggers. The model declined
  or said "I don't know"; failure mode but not actively wrong.
- ``CONFABULATION`` — at least one guard triggers (regardless of facts).
  The model produced forbidden content (e.g., wrong date, hallucinated
  source). The most severe failure mode and the one compaction strategies
  must avoid above all else.

A flat "passed/failed" boolean was the v0 scoring; severity is strictly
more informative. ``score.passed`` remains as a convenience property
mapped to ``severity == CLEAN_PASS`` for callers that only need a binary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from enum import Enum


# ── Public dataclasses ────────────────────────────────────────────────────────


class Severity(str, Enum):
    """Outcome bucket for a single assistant_target turn.

    String-valued so JSON serialization is trivial and stable across runs;
    the value is what shows up in baseline diffs.
    """

    CLEAN_PASS = "clean_pass"
    PARTIAL = "partial"
    NO_ANSWER = "no_answer"
    CONFABULATION = "confabulation"


@dataclass(frozen=True)
class FactCheckResult:
    """Per-fact match outcome for one ``expected_facts`` entry."""

    fact_id: str
    passed: bool
    detail: str = ""  # short reason for failure (or empty on pass)


@dataclass(frozen=True)
class GuardCheckResult:
    """Per-guard match outcome for one ``must_not_contain`` entry."""

    guard_id: str
    triggered: bool  # True means the forbidden pattern WAS found
    detail: str = ""


@dataclass
class TurnScore:
    """Combined mechanical score for a single assistant_target turn.

    ``severity`` is the load-bearing field; ``passed`` is the convenience
    binary derived from it. The per-fact / per-guard breakdown is preserved
    for reporting and debugging.
    """

    severity: Severity
    facts: list[FactCheckResult] = field(default_factory=list)
    guards: list[GuardCheckResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """True iff the response cleanly passed every fact with no guard
        trigger. Convenience for callers that only need a binary signal."""
        return self.severity is Severity.CLEAN_PASS

    @property
    def facts_passed(self) -> list[str]:
        return [f.fact_id for f in self.facts if f.passed]

    @property
    def facts_failed(self) -> list[str]:
        return [f.fact_id for f in self.facts if not f.passed]

    @property
    def guards_triggered(self) -> list[str]:
        return [g.guard_id for g in self.guards if g.triggered]


# ── Match implementations ─────────────────────────────────────────────────────


def _normalize(text: str) -> str:
    """Lowercase + collapse whitespace. Used by fuzzy match.

    Punctuation is preserved on purpose — fixture authors use specific
    punctuation in patterns ("10:47 PM", "March 12, 2024") and stripping
    it would create false-positive matches.
    """
    return re.sub(r"\s+", " ", text.strip()).lower()


def _regex_match(pattern: str, response: str) -> bool:
    """``True`` iff the regex matches anywhere in the response.

    Patterns may include inline flags like ``(?i)`` for case-insensitivity;
    we do not apply ``re.IGNORECASE`` globally to preserve fixture-author
    intent. Compilation errors are not caught here — bad fixture patterns
    should fail loudly during the test run, not silently return no-match.
    """
    return re.search(pattern, response) is not None


def _fuzzy_match(expected_text: str, response: str, min_score: float) -> tuple[bool, float]:
    """Token-aware fuzzy match.

    Returns ``(passed, observed_score)``. The score is the maximum
    ``SequenceMatcher`` ratio between the normalized expected text and any
    sliding window of equal length over the normalized response. This
    catches paraphrase-correct answers where the exact tokens are present
    but reordered or punctuated differently. Substring-equality short-
    circuits to score 1.0 since SequenceMatcher would compute the same.
    """
    norm_expected = _normalize(expected_text)
    norm_response = _normalize(response)

    if not norm_expected:
        return True, 1.0
    if norm_expected in norm_response:
        return True, 1.0

    expected_len = len(norm_expected)
    if expected_len > len(norm_response):
        # Response is shorter than expected; compare directly
        score = SequenceMatcher(None, norm_expected, norm_response).ratio()
        return score >= min_score, score

    # Sliding window of expected_len over the response, take max ratio.
    # Step by ~1/4 of the window to balance precision and runtime — exact
    # 1-step is O(n*m) which is fine for our short-response sizes but
    # noticeably faster on long responses.
    step = max(1, expected_len // 4)
    best = 0.0
    for i in range(0, len(norm_response) - expected_len + 1, step):
        window = norm_response[i : i + expected_len]
        score = SequenceMatcher(None, norm_expected, window).ratio()
        if score > best:
            best = score
            if best >= 1.0:
                break
    return best >= min_score, best


def _check_fact(fact: dict, response: str) -> FactCheckResult:
    fact_id = fact.get("id") or "<unnamed>"
    match_kind = fact.get("match", "regex")

    if match_kind == "regex":
        pattern = fact.get("pattern", "")
        passed = _regex_match(pattern, response)
        detail = "" if passed else f"regex did not match: {pattern!r}"
        return FactCheckResult(fact_id=fact_id, passed=passed, detail=detail)

    if match_kind == "fuzzy":
        expected = fact.get("text", "")
        min_score = float(fact.get("min_score", 0.75))
        passed, score = _fuzzy_match(expected, response, min_score)
        detail = "" if passed else f"fuzzy score {score:.2f} < {min_score:.2f} for {expected!r}"
        return FactCheckResult(fact_id=fact_id, passed=passed, detail=detail)

    return FactCheckResult(
        fact_id=fact_id,
        passed=False,
        detail=f"unknown match kind: {match_kind!r}",
    )


def _check_guard(guard: dict, response: str) -> GuardCheckResult:
    """A guard "triggers" when its pattern IS present in the response.

    Triggered guards mean the response contains forbidden content (e.g.,
    confabulated dates, topic-bleed terms). Triggered = bad.
    """
    guard_id = guard.get("id") or "<unnamed>"
    match_kind = guard.get("match", "regex")

    if match_kind == "regex":
        pattern = guard.get("pattern", "")
        triggered = _regex_match(pattern, response)
        detail = f"forbidden pattern matched: {pattern!r}" if triggered else ""
        return GuardCheckResult(guard_id=guard_id, triggered=triggered, detail=detail)

    if match_kind == "fuzzy":
        expected = guard.get("text", "")
        min_score = float(guard.get("min_score", 0.75))
        passed_match, score = _fuzzy_match(expected, response, min_score)
        # For guards we invert: passed_match = forbidden text was found
        triggered = passed_match
        detail = (
            f"forbidden fuzzy text matched at score {score:.2f}: {expected!r}"
            if triggered
            else ""
        )
        return GuardCheckResult(guard_id=guard_id, triggered=triggered, detail=detail)

    return GuardCheckResult(
        guard_id=guard_id,
        triggered=False,
        detail=f"unknown match kind: {match_kind!r}",
    )


# ── Public entry point ────────────────────────────────────────────────────────


def _classify_severity(
    facts: list[FactCheckResult], guards: list[GuardCheckResult]
) -> Severity:
    """Map fact + guard results to a single severity bucket.

    Order of precedence (worst first):
    1. Any guard triggered → CONFABULATION (the model produced forbidden
       content; severity dominates regardless of fact outcomes).
    2. All facts passed, no guards → CLEAN_PASS.
    3. Some but not all facts passed, no guards → PARTIAL.
    4. Zero facts passed, no guards → NO_ANSWER.

    A turn with no facts and no guards is treated as CLEAN_PASS — the
    fixture author is responsible for adding at least one fact when the
    response is supposed to assert something. This permits "smoke" turns
    that only check a guard.
    """
    if any(g.triggered for g in guards):
        return Severity.CONFABULATION
    if not facts:
        return Severity.CLEAN_PASS
    passed_count = sum(1 for f in facts if f.passed)
    if passed_count == len(facts):
        return Severity.CLEAN_PASS
    if passed_count == 0:
        return Severity.NO_ANSWER
    return Severity.PARTIAL


def score_turn(target_turn: dict, response: str) -> TurnScore:
    """Score one assistant_target turn's response against its fixture spec.

    ``target_turn`` is the fixture turn dict (must have role
    ``"assistant_target"``, with optional ``expected_facts`` and
    ``must_not_contain`` lists). ``response`` is the assembled assistant
    text returned by the chat model under the active strategy.
    """
    if target_turn.get("role") != "assistant_target":
        raise ValueError(
            f"score_turn called on non-target turn (role={target_turn.get('role')!r})"
        )

    facts = [_check_fact(f, response) for f in target_turn.get("expected_facts", [])]
    guards = [_check_guard(g, response) for g in target_turn.get("must_not_contain", [])]
    severity = _classify_severity(facts, guards)

    return TurnScore(severity=severity, facts=facts, guards=guards)
