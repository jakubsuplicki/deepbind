"""Tests for the mechanical scorer (ADR 010)."""

import pytest

from tests.eval.conversations.scorer import (
    FactCheckResult,
    GuardCheckResult,
    Severity,
    TurnScore,
    score_turn,
)


def _target(facts=None, guards=None) -> dict:
    """Build a minimal assistant_target fixture turn for scoring."""
    return {
        "role": "assistant_target",
        "expected_facts": facts or [],
        "must_not_contain": guards or [],
    }


# ── Regex matches ────────────────────────────────────────────────────────────


def test_regex_fact_passes_when_pattern_matches():
    turn = _target(facts=[{"id": "date", "match": "regex", "pattern": r"March 12, 2024"}])
    score = score_turn(turn, "The priority date is March 12, 2024.")
    assert score.passed
    assert score.facts_passed == ["date"]


def test_regex_fact_fails_when_pattern_misses():
    turn = _target(facts=[{"id": "date", "match": "regex", "pattern": r"March 12, 2024"}])
    score = score_turn(turn, "The priority date is March 13, 2024.")
    assert not score.passed
    assert score.facts_failed == ["date"]


def test_regex_inline_case_insensitive_flag():
    turn = _target(facts=[{"id": "topic", "match": "regex", "pattern": r"(?i)renewable\s+energy"}])
    assert score_turn(turn, "Renewable Energy is the topic.").passed
    assert score_turn(turn, "RENEWABLE ENERGY is the topic.").passed


# ── Fuzzy matches ────────────────────────────────────────────────────────────


def test_fuzzy_fact_passes_on_substring():
    turn = _target(facts=[{
        "id": "endpoint", "match": "fuzzy",
        "text": "30-day MACE rate", "min_score": 0.75,
    }])
    score = score_turn(turn, "The primary endpoint was 30-day MACE rate, adjudicated.")
    assert score.passed


def test_fuzzy_fact_passes_on_close_paraphrase():
    turn = _target(facts=[{
        "id": "endpoint", "match": "fuzzy",
        "text": "30-day MACE rate", "min_score": 0.55,
    }])
    # Slightly reworded — drop a hyphen, add words around it.
    score = score_turn(turn, "The primary endpoint measured 30 day MACE rate.")
    assert score.passed


def test_fuzzy_fact_fails_when_text_absent():
    turn = _target(facts=[{
        "id": "endpoint", "match": "fuzzy",
        "text": "30-day MACE rate", "min_score": 0.75,
    }])
    score = score_turn(turn, "The primary endpoint was cardiovascular mortality.")
    assert not score.passed


# ── Guards (must_not_contain) ────────────────────────────────────────────────


def test_guard_triggers_when_forbidden_pattern_present():
    turn = _target(
        facts=[{"id": "ok", "match": "regex", "pattern": "."}],
        guards=[{"id": "no_august", "match": "regex", "pattern": r"(?i)August\s+8"}],
    )
    score = score_turn(turn, "Filed on August 8, 2024 — the bearings patent.")
    assert not score.passed
    assert score.guards_triggered == ["no_august"]


def test_guard_does_not_trigger_when_pattern_absent():
    turn = _target(
        facts=[{"id": "ok", "match": "regex", "pattern": "."}],
        guards=[{"id": "no_august", "match": "regex", "pattern": r"(?i)August\s+8"}],
    )
    score = score_turn(turn, "Filed on March 12, 2024 — the bearings patent.")
    assert score.passed
    assert score.guards_triggered == []


# ── Combined invariant ───────────────────────────────────────────────────────


def test_turn_fails_when_any_fact_missing():
    turn = _target(facts=[
        {"id": "a", "match": "regex", "pattern": "alpha"},
        {"id": "b", "match": "regex", "pattern": "beta"},
    ])
    # Has "alpha", missing "beta" → fail
    score = score_turn(turn, "alpha was discussed.")
    assert not score.passed
    assert score.facts_passed == ["a"]
    assert score.facts_failed == ["b"]


def test_turn_fails_when_any_guard_triggers():
    turn = _target(
        facts=[{"id": "ok", "match": "regex", "pattern": "."}],
        guards=[
            {"id": "g1", "match": "regex", "pattern": "forbidden1"},
            {"id": "g2", "match": "regex", "pattern": "forbidden2"},
        ],
    )
    # Triggers g2 only → fail
    score = score_turn(turn, "the second pattern, forbidden2, is here.")
    assert not score.passed
    assert score.guards_triggered == ["g2"]


def test_turn_passes_when_all_facts_pass_and_no_guards_trigger():
    turn = _target(
        facts=[{"id": "a", "match": "regex", "pattern": "alpha"}],
        guards=[{"id": "g", "match": "regex", "pattern": "forbidden"}],
    )
    score = score_turn(turn, "alpha is fine.")
    assert score.passed
    assert score.facts_failed == []
    assert score.guards_triggered == []


def test_unknown_match_kind_fails_safe():
    """A typo in the fixture's match kind must fail the fact, not silently pass."""
    turn = _target(facts=[{"id": "x", "match": "regexp", "pattern": "alpha"}])
    score = score_turn(turn, "alpha is here.")
    assert not score.passed
    assert "unknown match kind" in score.facts[0].detail


def test_score_turn_rejects_non_target_turn():
    """Defensive: catches a runner bug that hands a user/scripted turn to the scorer."""
    with pytest.raises(ValueError, match="non-target turn"):
        score_turn({"role": "user", "content": "hi"}, "response")


# ── Severity classification ──────────────────────────────────────────────────


def test_severity_clean_pass_when_all_facts_pass_no_guards():
    turn = _target(facts=[
        {"id": "a", "match": "regex", "pattern": "alpha"},
        {"id": "b", "match": "regex", "pattern": "beta"},
    ])
    score = score_turn(turn, "alpha and beta both present")
    assert score.severity is Severity.CLEAN_PASS
    assert score.passed


def test_severity_partial_when_some_facts_pass_no_guards():
    turn = _target(facts=[
        {"id": "a", "match": "regex", "pattern": r"\balpha\b"},
        {"id": "b", "match": "regex", "pattern": r"\bbeta\b"},
    ])
    score = score_turn(turn, "alpha is present, the second item is missing")
    assert score.severity is Severity.PARTIAL
    assert not score.passed


def test_severity_no_answer_when_zero_facts_pass_no_guards():
    """A response that fails every fact but doesn't trigger a guard is a
    distinct failure mode from confabulation — typically 'I don't know'
    or 'I don't have that information'."""
    turn = _target(facts=[
        {"id": "a", "match": "regex", "pattern": "alpha"},
        {"id": "b", "match": "regex", "pattern": "beta"},
    ])
    score = score_turn(turn, "I don't have that information from our chat.")
    assert score.severity is Severity.NO_ANSWER
    assert not score.passed


def test_severity_confabulation_when_any_guard_triggers():
    """Guard trigger dominates regardless of fact outcomes — confabulation
    is the worst severity even if some facts also passed."""
    turn = _target(
        facts=[{"id": "a", "match": "regex", "pattern": "alpha"}],
        guards=[{"id": "no_x", "match": "regex", "pattern": "forbidden"}],
    )
    score = score_turn(turn, "alpha and forbidden both appear")
    assert score.severity is Severity.CONFABULATION
    assert not score.passed


def test_severity_confabulation_dominates_no_answer():
    """If a response triggers a guard AND fails all facts, classify as
    confabulation (the worst), not no_answer."""
    turn = _target(
        facts=[{"id": "a", "match": "regex", "pattern": "alpha"}],
        guards=[{"id": "no_x", "match": "regex", "pattern": "forbidden"}],
    )
    score = score_turn(turn, "the forbidden token is here, no alpha present")
    assert score.severity is Severity.CONFABULATION


def test_severity_clean_pass_for_smoke_turn_with_only_guards():
    """A turn with only must_not_contain (no expected_facts) is a 'smoke'
    turn — it passes cleanly as long as no guard triggers. This is allowed
    by the schema and useful for negative-only checks."""
    turn = _target(guards=[{"id": "no_x", "match": "regex", "pattern": "forbidden"}])
    score = score_turn(turn, "any harmless response")
    assert score.severity is Severity.CLEAN_PASS


def test_severity_string_value_is_stable_for_json():
    """Severity values must be JSON-stable strings so baseline diffs are
    readable across runs."""
    assert Severity.CLEAN_PASS.value == "clean_pass"
    assert Severity.PARTIAL.value == "partial"
    assert Severity.NO_ANSWER.value == "no_answer"
    assert Severity.CONFABULATION.value == "confabulation"


# ── Real fixture spot-check ──────────────────────────────────────────────────


def test_fixture_3_distractor_passes_on_correct_answer():
    """End-to-end check using fixture 3's actual expected_facts / guards.

    Confirms the real fixture spec rejects confabulation: a response that
    cites the distractor coating-patent date must fail the guard, while
    a response with the correct bearing-patent date must pass.
    """
    facts = [
        {"id": "bearing_priority_date", "match": "regex",
         "pattern": r"(?i)(March\s+12,?\s*2024|2024-03-12|3/12/2024|March\s+12th)"},
    ]
    guards = [
        {"id": "no_coating_date", "match": "regex",
         "pattern": r"(?i)August\s+8|2024-08-08|8/8/2024"},
        {"id": "no_made_up_date", "match": "regex",
         "pattern": r"(?i)(January|February|April|May|June|July|September|October|November|December)\s+\d{1,2},?\s*2024"},
    ]
    turn = _target(facts=facts, guards=guards)

    correct = "The bearing patent's priority date is March 12, 2024."
    confabulated = "The bearing patent's priority date is August 8, 2024."

    assert score_turn(turn, correct).passed
    assert not score_turn(turn, confabulated).passed


def test_dataclass_immutability_for_check_results():
    """FactCheckResult and GuardCheckResult are frozen — protects against
    the score map being mutated after scoring (e.g., for reporting)."""
    fact = FactCheckResult(fact_id="a", passed=True)
    guard = GuardCheckResult(guard_id="g", triggered=False)
    with pytest.raises(Exception):  # FrozenInstanceError on dataclass(frozen=True)
        fact.passed = False  # type: ignore[misc]
    with pytest.raises(Exception):
        guard.triggered = True  # type: ignore[misc]


def test_turn_score_aggregations_match_individual_results():
    """The convenience properties (facts_passed / facts_failed /
    guards_triggered) must agree with the individual check results."""
    score = TurnScore(
        severity=Severity.CONFABULATION,
        facts=[
            FactCheckResult("a", True),
            FactCheckResult("b", False),
            FactCheckResult("c", True),
        ],
        guards=[
            GuardCheckResult("g1", False),
            GuardCheckResult("g2", True),
        ],
    )
    assert set(score.facts_passed) == {"a", "c"}
    assert score.facts_failed == ["b"]
    assert score.guards_triggered == ["g2"]
    # passed property derives from severity
    assert not score.passed
