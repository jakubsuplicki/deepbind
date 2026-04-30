"""Tests for ADR 009 system-prompt budget enforcement.

The pre-ADR-009 ``build_system_prompt_with_stats`` truncated each
retrieved note individually but had no total cap. A retrieval that
surfaced too many notes could push the assembled prompt past the
model's safe ceiling. The ADR adds an optional total-budget kwarg that
truncates the retrieved-context block (not the base persona, not the
language reminder) until the prompt fits.

These tests pin:

- No budget supplied → existing behavior unchanged.
- Budget supplied + prompt already fits → no truncation.
- Budget supplied + prompt over budget → context truncated; the
  ``context_truncated`` stat is True.
- Pathological budget (smaller than base + reminder) drops the context
  entirely without raising.
"""

from __future__ import annotations

import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import pytest

from services.system_prompt import _enforce_system_prompt_budget


def test_no_budget_returns_context_unchanged():
    out, truncated, _bt, _ct, _lt = _enforce_system_prompt_budget(
        base="b" * 100,
        context="c" * 5000,
        lang_reminder="r" * 100,
        budget_tokens=None,
        tokenizer_id=None,
    )
    assert out == "c" * 5000
    assert truncated is False


def test_zero_budget_returns_unchanged():
    out, truncated, _bt, _ct, _lt = _enforce_system_prompt_budget(
        base="b" * 100,
        context="c" * 5000,
        lang_reminder="r" * 100,
        budget_tokens=0,
        tokenizer_id=None,
    )
    assert out == "c" * 5000
    assert truncated is False


def test_empty_context_returns_empty_unchanged():
    out, truncated, _bt, _ct, _lt = _enforce_system_prompt_budget(
        base="b" * 100,
        context=None,
        lang_reminder="r" * 100,
        budget_tokens=200,
        tokenizer_id=None,
    )
    assert out is None
    assert truncated is False


def test_under_budget_returns_unchanged():
    # base + lang_reminder + context all under budget → no truncation.
    out, truncated, _bt, _ct, _lt = _enforce_system_prompt_budget(
        base="b" * 40,    # 10 tokens (char/4)
        context="c" * 40, # 10 tokens
        lang_reminder="r" * 40,  # 10 tokens
        budget_tokens=100,
        tokenizer_id=None,
    )
    assert out == "c" * 40
    assert truncated is False


def test_over_budget_truncates_context():
    # base = 40 chars = 10 tok, lang = 40 chars = 10 tok, budget = 50 tok.
    # context budget = 50 - 20 = 30 tok = ~120 chars. Original context
    # is 800 chars (200 tok) → must be truncated.
    out, truncated, _bt, _ct, _lt = _enforce_system_prompt_budget(
        base="b" * 40,
        context="c" * 800,
        lang_reminder="r" * 40,
        budget_tokens=50,
        tokenizer_id=None,
    )
    assert truncated is True
    assert len(out) < 800
    assert "truncated" in out


def test_pathological_budget_drops_context():
    """When base + lang_reminder alone exceed the budget, drop context."""
    out, truncated, _bt, _ct, _lt = _enforce_system_prompt_budget(
        base="b" * 1000,
        context="c" * 1000,
        lang_reminder="r" * 1000,
        budget_tokens=10,
        tokenizer_id=None,
    )
    assert out == ""
    assert truncated is True


def test_helper_returns_token_counts_when_budget_runs():
    """The helper returns the base/context/lang token counts it
    already computed so the caller's stats block can avoid three
    redundant tokenizer encodes per turn."""
    out, truncated, base_tok, context_tok, lang_tok = _enforce_system_prompt_budget(
        base="b" * 40,
        context="c" * 80,
        lang_reminder="r" * 40,
        budget_tokens=200,
        tokenizer_id=None,
    )
    assert truncated is False
    assert out == "c" * 80
    # Counts use the char/4 fallback since tokenizer_id is None.
    assert base_tok == 10
    assert context_tok == 20
    assert lang_tok == 10


def test_helper_returns_none_counts_when_budget_short_circuits():
    """When budget enforcement short-circuits (no budget supplied or
    no context to truncate), the helper returns None for the cached
    counts so the caller knows to compute them itself."""
    out, truncated, base_tok, context_tok, lang_tok = _enforce_system_prompt_budget(
        base="b" * 100,
        context="c" * 100,
        lang_reminder="r" * 100,
        budget_tokens=None,
        tokenizer_id=None,
    )
    assert truncated is False
    assert base_tok is None
    assert context_tok is None
    assert lang_tok is None


def test_truncated_output_plus_marker_fits_budget():
    """ADR 009 invariant: the assembled output (truncated context +
    marker) must always fit within the context budget. A previous
    implementation appended the marker after the retry loop without
    re-checking, allowing over-budget content to ship silently.
    """
    from services.token_counting import count_tokens

    base_tokens_target = 10  # 40 chars
    lang_tokens_target = 5   # 20 chars
    budget = 30  # tokens; context_budget = 30 - 15 = 15 tokens
    out, truncated, _bt, _ct, _lt = _enforce_system_prompt_budget(
        base="b" * (base_tokens_target * 4),
        context="c" * 4000,  # massively over
        lang_reminder="r" * (lang_tokens_target * 4),
        budget_tokens=budget,
        tokenizer_id=None,
    )
    assert truncated is True
    if out:
        # Truncated context plus marker must fit the post-overhead budget.
        context_budget = budget - base_tokens_target - lang_tokens_target
        assert count_tokens(out, tokenizer_id=None) <= context_budget
    # Either dropped entirely or truncated to fit — never over-budget.
