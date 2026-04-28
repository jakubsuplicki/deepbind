"""Tests for the conversation-eval CLI (ADR 010, final chunk)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.chat import FullHistoryStrategy

from tests.eval.conversations.run_eval import (
    build_arg_parser,
    build_output,
    discover_fixtures,
    parse_seeds,
    parse_strategies,
    parse_strategy,
    run_all_strategies,
)
from tests.eval.conversations.runner import load_fixture
from tests.eval.conversations.strategies import (
    NaiveTruncateStrategy,
    RetrievalSubstitutionV1Strategy,
)


_FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ── Strategy-string parsing ─────────────────────────────────────────────────


def test_parse_strategy_full_history():
    s = parse_strategy("full-history")
    assert isinstance(s, FullHistoryStrategy)
    assert s.name == "full-history"


def test_parse_strategy_naive_truncate_with_n():
    s = parse_strategy("naive-truncate-12")
    assert isinstance(s, NaiveTruncateStrategy)
    assert s.recent_n == 12
    assert s.name == "naive-truncate-12"


def test_parse_strategy_naive_truncate_rejects_non_integer():
    with pytest.raises(ValueError, match="invalid naive-truncate suffix"):
        parse_strategy("naive-truncate-eight")


def test_parse_strategy_rejects_unknown_name():
    """Unknown family name (v9 is not the supported v1)."""
    with pytest.raises(ValueError, match="unknown strategy"):
        parse_strategy("retrieval-substitution-v9-n4-k2")


def test_parse_strategy_retrieval_substitution_v1():
    s = parse_strategy("retrieval-substitution-v1-n8-k3")
    assert isinstance(s, RetrievalSubstitutionV1Strategy)
    assert s.recent_n == 8
    assert s.top_k == 3
    assert s.name == "retrieval-substitution-v1-n8-k3"


def test_parse_strategy_retrieval_substitution_rejects_malformed_suffix():
    with pytest.raises(ValueError, match="invalid retrieval-substitution suffix"):
        parse_strategy("retrieval-substitution-v1-n8")  # no -k
    with pytest.raises(ValueError, match="invalid retrieval-substitution suffix"):
        parse_strategy("retrieval-substitution-v1-nfoo-kbar")


def test_parse_strategies_comma_separated():
    out = parse_strategies("full-history,naive-truncate-4,naive-truncate-8")
    assert [s.name for s in out] == [
        "full-history",
        "naive-truncate-4",
        "naive-truncate-8",
    ]


def test_parse_strategies_rejects_empty():
    with pytest.raises(ValueError, match="non-empty"):
        parse_strategies("")
    with pytest.raises(ValueError, match="non-empty"):
        parse_strategies(",,,")


def test_parse_strategies_strips_whitespace():
    out = parse_strategies("  full-history , naive-truncate-4  ")
    assert [s.name for s in out] == ["full-history", "naive-truncate-4"]


# ── Seed parsing ─────────────────────────────────────────────────────────────


def test_parse_seeds_comma_separated():
    assert parse_seeds("1,2,3") == [1, 2, 3]


def test_parse_seeds_strips_whitespace():
    assert parse_seeds(" 7 , 8 , 9 ") == [7, 8, 9]


def test_parse_seeds_rejects_empty():
    with pytest.raises(ValueError, match="non-empty"):
        parse_seeds("")


def test_parse_seeds_rejects_duplicates():
    with pytest.raises(ValueError, match="unique"):
        parse_seeds("1,1,2")


def test_parse_seeds_rejects_non_integer():
    with pytest.raises(ValueError, match="invalid seed"):
        parse_seeds("1,two,3")


# ── Fixture discovery ────────────────────────────────────────────────────────


def test_discover_fixtures_returns_only_fixture_jsons():
    paths = discover_fixtures(_FIXTURES_DIR)
    names = [p.name for p in paths]
    assert all(n.endswith(".json") for n in names)
    assert not any(n.endswith(".tools.json") for n in names)


def test_discover_fixtures_finds_all_launch_fixtures():
    """The fixture suite has grown beyond the original 10 launch fixtures.
    Pin a lower bound so accidental deletions still break this test."""
    paths = discover_fixtures(_FIXTURES_DIR)
    assert len(paths) >= 15


def test_discover_fixtures_returns_sorted_paths():
    """Sorted order is what makes the baseline JSON's per-fixture
    section stable across runs — protect that property."""
    paths = discover_fixtures(_FIXTURES_DIR)
    assert paths == sorted(paths)


def test_discover_fixtures_raises_on_empty_dir(tmp_path):
    with pytest.raises(FileNotFoundError, match="no fixtures found"):
        discover_fixtures(tmp_path)


# ── Depth-pressure fixtures (16-19) ──────────────────────────────────────────


_DEPTH_PRESSURE_IDS = [
    "deep-name-recall",
    "deep-number-recall",
    "deep-decision-recall",
    "deep-multi-detail-recall",
]


@pytest.mark.parametrize("fixture_id", _DEPTH_PRESSURE_IDS)
def test_depth_pressure_fixture_has_more_than_16_user_turns(fixture_id):
    """Each depth-pressure fixture must have > 16 real user turns. With
    16 or fewer, naive-truncate-16 returns the full history and the
    fixture stops being depth-pressure — silently weakens the gate."""
    from tests.eval.conversations.strategies import _is_real_user_turn
    paths = discover_fixtures(_FIXTURES_DIR)
    fx = next(load_fixture(p) for p in paths if load_fixture(p)["id"] == fixture_id)

    # Replay turns into the runner-shape history that NaiveTruncate sees
    history: list[dict] = []
    for t in fx["turns"]:
        if t.get("role") == "user":
            history.append({"role": "user", "content": t.get("content", "")})
        elif t.get("role") == "assistant_scripted":
            history.append({"role": "assistant", "content": t.get("content", "")})
        elif t.get("role") == "assistant_target":
            break
    real_user_turns = sum(1 for m in history if _is_real_user_turn(m))
    assert real_user_turns > 16, (
        f"{fixture_id}: only {real_user_turns} real user turns; "
        "naive-truncate-16 will return full history and the fixture won't stress depth"
    )


@pytest.mark.parametrize("fixture_id", _DEPTH_PRESSURE_IDS)
def test_depth_pressure_fixture_loses_target_content_under_naive_16(fixture_id):
    """The buried detail must actually disappear when naive-truncate-16
    runs. Pin this with a fixture-specific anchor token: each fixture
    has a token that should show up only in the early "founding" turn,
    not anywhere in the trailing distractor chat."""
    paths = discover_fixtures(_FIXTURES_DIR)
    fx = next(load_fixture(p) for p in paths if load_fixture(p)["id"] == fixture_id)

    # Reconstruct history (sans assistant_target, as the runner would)
    history: list[dict] = []
    for t in fx["turns"]:
        if t.get("role") == "user":
            history.append({"role": "user", "content": t.get("content", "")})
        elif t.get("role") == "assistant_scripted":
            history.append({"role": "assistant", "content": t.get("content", "")})
        elif t.get("role") == "assistant_target":
            break

    # Token that should only appear in the founding turn (chosen per fixture)
    anchor_by_id = {
        "deep-name-recall": "albatross",
        "deep-number-recall": "47,500",
        "deep-decision-recall": "postgresql",
        "deep-multi-detail-recall": "northwind",
    }
    anchor = anchor_by_id[fixture_id]

    full_blob = " ".join(
        m["content"] for m in history if isinstance(m.get("content"), str)
    ).lower()
    assert anchor.lower() in full_blob, (
        f"{fixture_id}: anchor token {anchor!r} not present in full history — "
        "fixture authoring drift"
    )

    # After naive-truncate-16, the anchor must be GONE.
    out = NaiveTruncateStrategy(recent_n=16).assemble(history)
    truncated_blob = " ".join(
        m["content"] for m in out if isinstance(m.get("content"), str)
    ).lower()
    assert anchor.lower() not in truncated_blob, (
        f"{fixture_id}: anchor token {anchor!r} still present after naive-truncate-16; "
        "fixture is not actually depth-pressure"
    )


@pytest.mark.parametrize("fixture_id", _DEPTH_PRESSURE_IDS)
def test_depth_pressure_fixture_recoverable_under_retrieval_substitution(fixture_id):
    """The hypothesis the gate tests: retrieval-substitution-v1 should
    re-introduce the buried detail via keyword overlap with the latest
    user turn (which references the topic). Pin that the strategy does
    in fact find and re-introduce the founding turn — so any later eval
    failure can be attributed to model behavior, not strategy mechanics."""
    paths = discover_fixtures(_FIXTURES_DIR)
    fx = next(load_fixture(p) for p in paths if load_fixture(p)["id"] == fixture_id)

    history: list[dict] = []
    for t in fx["turns"]:
        if t.get("role") == "user":
            history.append({"role": "user", "content": t.get("content", "")})
        elif t.get("role") == "assistant_scripted":
            history.append({"role": "assistant", "content": t.get("content", "")})
        elif t.get("role") == "assistant_target":
            break

    anchor_by_id = {
        "deep-name-recall": "albatross",
        "deep-number-recall": "47,500",
        "deep-decision-recall": "postgresql",
        "deep-multi-detail-recall": "northwind",
    }
    anchor = anchor_by_id[fixture_id]

    strategy = RetrievalSubstitutionV1Strategy(recent_n=4, top_k=3, min_overlap=1)
    out = strategy.assemble(history)
    assembled_blob = " ".join(
        m["content"] for m in out if isinstance(m.get("content"), str)
    ).lower()
    assert anchor.lower() in assembled_blob, (
        f"{fixture_id}: retrieval-substitution-v1 failed to re-introduce "
        f"anchor token {anchor!r} — strategy can't be a fair test against "
        "naive-truncate at this N if it doesn't surface the buried detail"
    )


# ── Argparse wiring ──────────────────────────────────────────────────────────


def test_arg_parser_defaults_provider_to_ollama():
    parser = build_arg_parser()
    args = parser.parse_args([])
    assert args.provider == "ollama"


def test_arg_parser_default_strategies_includes_full_and_naive_sweep():
    parser = build_arg_parser()
    args = parser.parse_args([])
    strategies = parse_strategies(args.strategies)
    names = [s.name for s in strategies]
    assert "full-history" in names
    assert any(n.startswith("naive-truncate-") for n in names)
    # Sweep multiple values
    naive_count = sum(1 for n in names if n.startswith("naive-truncate-"))
    assert naive_count >= 2, "default strategies must sweep multiple N values"


def test_arg_parser_default_strategies_includes_retrieval_substitution():
    """The default sweep must include retrieval-substitution-v1 — that's
    the strategy ADR 010's gate exists to evaluate. Forgetting it here
    would silently let the gate degenerate to the previous full-vs-naive-only
    comparison."""
    parser = build_arg_parser()
    args = parser.parse_args([])
    names = [s.name for s in parse_strategies(args.strategies)]
    assert any(n.startswith("retrieval-substitution-v1-") for n in names)


def test_arg_parser_default_includes_matched_naive_for_each_retrieval():
    """For every retrieval-substitution-v1-nN-kK in the default sweep,
    the matching naive-truncate-N must also be present, since the
    retrieval-vs-naive gate compares them at matched N. If the defaults
    drift apart, the gate output silently loses comparisons."""
    parser = build_arg_parser()
    args = parser.parse_args([])
    names = [s.name for s in parse_strategies(args.strategies)]
    for n in names:
        if n.startswith("retrieval-substitution-v1-n"):
            n_value = n.split("-n", 1)[1].split("-k", 1)[0]
            naive_match = f"naive-truncate-{n_value}"
            assert naive_match in names, (
                f"retrieval strategy {n!r} has no matching {naive_match!r} "
                f"in defaults — the gate cannot pair them"
            )


def test_arg_parser_retrieval_flag_defaults_off():
    parser = build_arg_parser()
    args = parser.parse_args([])
    assert args.retrieval is False


def test_arg_parser_retrieval_flag_can_be_enabled():
    parser = build_arg_parser()
    args = parser.parse_args(["--retrieval"])
    assert args.retrieval is True


def test_arg_parser_seeds_default_is_three():
    parser = build_arg_parser()
    args = parser.parse_args([])
    assert parse_seeds(args.seeds) == [1, 2, 3]


# ── End-to-end with stub chat (no network) ───────────────────────────────────


@pytest.mark.asyncio
async def test_run_all_strategies_against_stub_chat():
    """Sanity that the orchestration glue runs every fixture × strategy
    × seed combo with a stub chat factory and produces the right shape."""
    from tests.eval.conversations.chat_adapters import OllamaChat

    class _StubChat:
        async def __call__(self, messages, system_prompt):
            return "stub response — no real model in tests"

        @property
        def model_id(self):
            return "stub:test"

    def factory(seed: int):
        return _StubChat()

    # Use only 2 fixtures to keep the test fast
    paths = discover_fixtures(_FIXTURES_DIR)[:2]

    results = await run_all_strategies(
        paths,
        [FullHistoryStrategy(), NaiveTruncateStrategy(recent_n=4)],
        chat_factory=factory,
        seeds=[1, 2],
        chat_model_id="stub:test",
    )
    assert sorted(results.keys()) == ["full-history", "naive-truncate-4"]
    for fx_results in results.values():
        assert len(fx_results) == 2  # 2 fixtures
        for r in fx_results:
            # 2 seeds × at least 1 target turn
            assert len(r.seeds) == 2
            assert r.target_turn_count >= 1


# ── Output JSON shape ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_output_produces_stable_keyed_json():
    """The output JSON must have all keys sorted at every nesting level
    so ``git diff`` between runs is readable. Pin the top-level shape
    here; nested orderings are exercised by the orchestration test
    above."""
    from tests.eval.conversations.runner import (
        FixtureResult,
        TurnResult,
    )
    from tests.eval.conversations.scorer import (
        FactCheckResult,
        Severity,
        TurnScore,
    )

    def _r(fixture_id: str, strategy_name: str, passed: bool):
        return FixtureResult(
            fixture_id=fixture_id,
            strategy_name=strategy_name,
            chat_model_id="stub",
            target_turn_count=1,
            seeds=[1, 2],
            turn_results=[
                TurnResult(
                    turn_index=0,
                    seed=s,
                    response_text="x",
                    score=TurnScore(
                        severity=(
                            Severity.CLEAN_PASS if passed else Severity.NO_ANSWER
                        ),
                        facts=[FactCheckResult("a", passed)],
                    ),
                    latency_ms=1.0,
                )
                for s in [1, 2]
            ],
        )

    results = {
        "full-history": [_r(f"fx{i}", "full-history", True) for i in range(3)],
        "naive-truncate-8": [_r(f"fx{i}", "naive-truncate-8", True) for i in range(3)],
    }
    output = build_output(
        results=results,
        chat_model_id="stub",
        seeds=[1, 2],
        fixture_ids=["fx0", "fx1", "fx2"],
        retrieval_enabled=False,
    )

    # Top-level keys present
    assert set(output.keys()) == {
        "run_metadata",
        "overall",
        "gate_decisions",
        "by_strategy",
    }
    # Run metadata records the strategies, fixture count, seed count
    md = output["run_metadata"]
    assert md["seeds"] == [1, 2]
    assert md["fixture_count"] == 3
    assert md["seed_count"] == 2
    assert md["retrieval_enabled"] is False

    # Overall has both strategies
    assert "full-history" in output["overall"]
    assert "naive-truncate-8" in output["overall"]

    # Gate decisions: full-history vs naive-truncate-8 should be
    # computed and include the naive_vs_full_history sub-key
    assert "full-history__vs__naive-truncate-8" in output["gate_decisions"]
    decision = output["gate_decisions"]["full-history__vs__naive-truncate-8"][
        "naive_vs_full_history"
    ]
    assert "verdict" in decision
    assert "mean_difference" in decision

    # by_strategy results are sorted by fixture_id
    for strategy_name, fx_results in output["by_strategy"].items():
        ids = [r["fixture_id"] for r in fx_results]
        assert ids == sorted(ids), f"{strategy_name} fixture order is unstable"


def test_build_output_pairs_retrieval_with_matched_naive_for_gate():
    """The gate must compute retrieval-vs-naive at matched N. Confirms
    the JSON output contains the right gate-decision keys when both
    strategies are in the run."""
    from tests.eval.conversations.runner import FixtureResult, TurnResult
    from tests.eval.conversations.scorer import (
        FactCheckResult,
        Severity,
        TurnScore,
    )

    def _r(fixture_id: str, strategy_name: str, passed: bool):
        return FixtureResult(
            fixture_id=fixture_id,
            strategy_name=strategy_name,
            chat_model_id="stub",
            target_turn_count=1,
            seeds=[1],
            turn_results=[
                TurnResult(
                    turn_index=0,
                    seed=1,
                    response_text="x",
                    score=TurnScore(
                        severity=(
                            Severity.CLEAN_PASS if passed else Severity.NO_ANSWER
                        ),
                        facts=[FactCheckResult("a", passed)],
                    ),
                    latency_ms=1.0,
                )
            ],
        )

    # Build 8 fixtures so the gate's min-fixtures floor doesn't fire
    # (compare_strategies has min_fixtures=5; we use 8 to be comfortable).
    fixture_ids = [f"fx{i}" for i in range(8)]
    results = {
        "full-history": [_r(fid, "full-history", True) for fid in fixture_ids],
        "naive-truncate-8": [
            _r(fid, "naive-truncate-8", True) for fid in fixture_ids
        ],
        "retrieval-substitution-v1-n8-k3": [
            _r(fid, "retrieval-substitution-v1-n8-k3", True) for fid in fixture_ids
        ],
    }
    output = build_output(
        results=results,
        chat_model_id="stub",
        seeds=[1],
        fixture_ids=fixture_ids,
        retrieval_enabled=False,
    )

    gd = output["gate_decisions"]
    # Both gate families must be present
    assert "full-history__vs__naive-truncate-8" in gd
    assert "naive-truncate-8__vs__retrieval-substitution-v1-n8-k3" in gd

    # The retrieval-vs-naive comparison uses the renamed sub-key
    retrieval_decision = gd[
        "naive-truncate-8__vs__retrieval-substitution-v1-n8-k3"
    ]["retrieval_vs_naive"]
    assert "verdict" in retrieval_decision
    assert "mean_difference" in retrieval_decision
    # All-pass on both sides → mean_difference is zero
    assert retrieval_decision["mean_difference"] == 0.0


def test_build_output_skips_retrieval_gate_when_naive_n_does_not_match():
    """If the retrieval strategy is at N=8 but only N=4 naive is present,
    the retrieval-vs-naive comparison must NOT be silently emitted with
    a wrong pairing — that would be a meaningless comparison."""
    from tests.eval.conversations.runner import FixtureResult, TurnResult
    from tests.eval.conversations.scorer import (
        FactCheckResult,
        Severity,
        TurnScore,
    )

    def _r(fixture_id: str, strategy_name: str):
        return FixtureResult(
            fixture_id=fixture_id,
            strategy_name=strategy_name,
            chat_model_id="stub",
            target_turn_count=1,
            seeds=[1],
            turn_results=[
                TurnResult(
                    turn_index=0,
                    seed=1,
                    response_text="x",
                    score=TurnScore(
                        severity=Severity.CLEAN_PASS,
                        facts=[FactCheckResult("a", True)],
                    ),
                    latency_ms=1.0,
                )
            ],
        )

    fixture_ids = [f"fx{i}" for i in range(8)]
    results = {
        "naive-truncate-4": [_r(fid, "naive-truncate-4") for fid in fixture_ids],
        "retrieval-substitution-v1-n8-k3": [
            _r(fid, "retrieval-substitution-v1-n8-k3") for fid in fixture_ids
        ],
    }
    output = build_output(
        results=results,
        chat_model_id="stub",
        seeds=[1],
        fixture_ids=fixture_ids,
        retrieval_enabled=False,
    )
    # No gate decision involving the retrieval strategy at all (no matched N)
    for key in output["gate_decisions"].keys():
        assert "retrieval-substitution-v1-n8-k3" not in key, (
            f"expected no retrieval-vs-naive pair when N is unmatched, got {key}"
        )


def test_build_output_serializes_severity_as_string():
    """Severity values must be strings in the JSON output, not enum
    objects — JSON serialization would fail otherwise."""
    from tests.eval.conversations.runner import FixtureResult, TurnResult
    from tests.eval.conversations.scorer import (
        FactCheckResult,
        Severity,
        TurnScore,
    )

    fr = FixtureResult(
        fixture_id="fx",
        strategy_name="full-history",
        chat_model_id="stub",
        target_turn_count=1,
        seeds=[1],
        turn_results=[
            TurnResult(
                turn_index=0,
                seed=1,
                response_text="ok",
                score=TurnScore(
                    severity=Severity.CONFABULATION,
                    facts=[FactCheckResult("a", False)],
                ),
                latency_ms=1.0,
            )
        ],
    )
    output = build_output(
        results={"full-history": [fr]},
        chat_model_id="stub",
        seeds=[1],
        fixture_ids=["fx"],
        retrieval_enabled=False,
    )
    # Round-trip through json to confirm serializable
    rendered = json.dumps(output)
    assert "confabulation" in rendered
    # Severity must NOT serialize as the enum repr
    assert "Severity." not in rendered
