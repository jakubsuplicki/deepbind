"""Unit tests for ingest scenario shape (ADR 013)."""

from __future__ import annotations

from pathlib import Path

from tests.eval.ingest.scenarios import (
    DEFAULT_FIXTURE_RELATIVE,
    END_TO_END_STAGE_ORDER,
    Stage,
    chunk_only,
    default_scenarios,
    embed_batch,
    end_to_end,
    entity_extract_only,
    extract_only,
    section_detect_only,
)


def test_default_scenarios_covers_every_stage():
    fx = Path("/tmp/fake.pdf")
    scenarios = default_scenarios(fx)
    stages = {s.stage for s in scenarios}
    assert stages == {
        Stage.END_TO_END,
        Stage.EXTRACT,
        Stage.SECTION_DETECT,
        Stage.CHUNK,
        Stage.EMBED_BATCH,
        Stage.ENTITY_EXTRACT,
    }


def test_scenario_names_are_unique():
    fx = Path("/tmp/fake.pdf")
    scenarios = default_scenarios(fx)
    names = [s.name for s in scenarios]
    assert len(names) == len(set(names))


def test_scenario_names_include_fixture_stem():
    fx = Path("/tmp/somefile.pdf")
    s = end_to_end(fx)
    assert "somefile" in s.name


def test_embed_batch_records_batch_size_in_name():
    fx = Path("/tmp/x.pdf")
    s = embed_batch(fx, batch_size=128)
    assert "128" in s.name
    assert s.embed_batch_size == 128


def test_end_to_end_stage_order_is_pipeline_order():
    """The end-to-end stage order list must match the production pipeline:
    extract → sections → chunk → embed → entities. Reordering these is a
    semantic change (later stages depend on earlier outputs).
    """
    assert END_TO_END_STAGE_ORDER == (
        "extract",
        "section_detect",
        "chunk",
        "embed_batch",
        "entity_extract",
    )


def test_default_fixture_relative_points_at_samples():
    assert str(DEFAULT_FIXTURE_RELATIVE).startswith("samples/")
    assert str(DEFAULT_FIXTURE_RELATIVE).endswith(".pdf")


def test_isolated_scenarios_carry_correct_stage():
    fx = Path("/tmp/x.pdf")
    assert extract_only(fx).stage is Stage.EXTRACT
    assert section_detect_only(fx).stage is Stage.SECTION_DETECT
    assert chunk_only(fx).stage is Stage.CHUNK
    assert embed_batch(fx).stage is Stage.EMBED_BATCH
    assert entity_extract_only(fx).stage is Stage.ENTITY_EXTRACT


def test_scenario_is_frozen():
    """IngestScenario is frozen so passing one to multiple harness calls
    can't accidentally mutate it."""
    import dataclasses

    fx = Path("/tmp/x.pdf")
    s = end_to_end(fx)
    try:
        # frozen dataclass: assignment must raise FrozenInstanceError
        s.name = "mutated"  # type: ignore[misc]
        assert False, "frozen scenario should reject mutation"
    except dataclasses.FrozenInstanceError:
        pass
