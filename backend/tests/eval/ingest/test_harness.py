"""Unit tests for ingest harness stage primitives (ADR 013).

These tests exercise the harness without loading the real fastembed or
spaCy models — that machinery is wrapped behind monkeypatched stubs.
The harness itself is a thin wrapper around the production functions,
so the tests focus on (a) timing capture, (b) units metadata, (c) the
end-to-end run order, and (d) error propagation.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.eval.ingest.harness import (
    IngestHarness,
    IngestRun,
    PreparedInputs,
    StageTiming,
    run_end_to_end,
)
from tests.eval.ingest.scenarios import (
    Stage,
    chunk_only,
    embed_batch,
    end_to_end,
    entity_extract_only,
    extract_only,
    section_detect_only,
)


@dataclass
class _FakeChunk:
    """Mimic services.chunking.Chunk.text shape for tests."""

    text: str = "stub chunk"


def _patch_stages(monkeypatch, *, fail_on: str | None = None) -> dict[str, int]:
    """Monkeypatch each production stage to a fast deterministic stub.

    Returns a dict with call counts per stage so tests can assert
    "stage X was called once" without timing dependence.
    """
    counts = {
        "extract": 0,
        "section_detect": 0,
        "chunk": 0,
        "embed": 0,
        "entity": 0,
    }

    def fake_extract(path):
        counts["extract"] += 1
        if fail_on == "extract":
            raise RuntimeError("boom-extract")
        return "Section One\n\nbody one\n\nSection Two\n\nbody two"

    def fake_detect(text):
        counts["section_detect"] += 1
        if fail_on == "section_detect":
            raise RuntimeError("boom-sections")
        return [object(), object()]

    def fake_chunk(content, **_kwargs):
        counts["chunk"] += 1
        if fail_on == "chunk":
            raise RuntimeError("boom-chunk")
        return [_FakeChunk("a"), _FakeChunk("b"), _FakeChunk("c")]

    def fake_embed(texts):
        counts["embed"] += 1
        if fail_on == "embed":
            raise RuntimeError("boom-embed")
        return [[0.1, 0.2, 0.3] for _ in texts]

    def fake_entities(text, existing_people=None):
        counts["entity"] += 1
        if fail_on == "entity":
            raise RuntimeError("boom-entity")
        return [object()]

    monkeypatch.setattr("services.ingest._extract_pdf_text", fake_extract)
    monkeypatch.setattr("services.ingest._detect_pdf_sections", fake_detect)
    monkeypatch.setattr("services.chunking.chunk_markdown", fake_chunk)
    monkeypatch.setattr("services.embedding_service.embed_texts", fake_embed)
    monkeypatch.setattr("services.entity_extraction.extract_entities", fake_entities)
    return counts


def test_run_end_to_end_invokes_every_stage_once(monkeypatch, tmp_path):
    counts = _patch_stages(monkeypatch)
    fx = tmp_path / "x.pdf"
    fx.write_bytes(b"%PDF-1.4\nfake")

    run = run_end_to_end(fx, embed_batch_size=2)
    assert run.error is None
    assert counts == {
        "extract": 1,
        "section_detect": 1,
        "chunk": 1,
        "embed": 1,
        "entity": 1,
    }


def test_run_end_to_end_stage_order_is_pipeline_order(monkeypatch, tmp_path):
    _patch_stages(monkeypatch)
    fx = tmp_path / "x.pdf"
    fx.write_bytes(b"%PDF-1.4\nfake")

    run = run_end_to_end(fx, embed_batch_size=2)
    names = [s.name for s in run.stages]
    assert names == [
        "extract",
        "section_detect",
        "chunk",
        "embed_batch",
        "entity_extract",
    ]


def test_run_end_to_end_total_ms_is_positive(monkeypatch, tmp_path):
    _patch_stages(monkeypatch)
    fx = tmp_path / "x.pdf"
    fx.write_bytes(b"%PDF-1.4\nfake")

    run = run_end_to_end(fx, embed_batch_size=2)
    assert run.total_ms > 0
    # Sum of stage durations must be ≤ total (small overhead allowed)
    stage_sum = sum(s.duration_ms for s in run.stages)
    assert stage_sum <= run.total_ms + 1.0  # tolerance for harness overhead


def test_run_end_to_end_captures_units(monkeypatch, tmp_path):
    _patch_stages(monkeypatch)
    fx = tmp_path / "x.pdf"
    fx.write_bytes(b"%PDF-1.4\nfake")

    run = run_end_to_end(fx, embed_batch_size=2)
    by_stage = {s.name: s for s in run.stages}
    assert by_stage["extract"].units["chars"] > 0
    assert by_stage["section_detect"].units["sections_detected"] == 2
    assert by_stage["chunk"].units["chunks_emitted"] == 3
    assert by_stage["embed_batch"].units["batch_size"] == 2  # capped to 2 of 3
    assert by_stage["entity_extract"].units["entities_extracted"] == 1


def test_run_end_to_end_surfaces_error_without_crashing(monkeypatch, tmp_path):
    _patch_stages(monkeypatch, fail_on="chunk")
    fx = tmp_path / "x.pdf"
    fx.write_bytes(b"%PDF-1.4\nfake")

    run = run_end_to_end(fx, embed_batch_size=2)
    assert run.error is not None
    assert "boom-chunk" in run.error
    # Stages before the failure should still be present
    names = [s.name for s in run.stages]
    assert "extract" in names
    assert "section_detect" in names
    # Stages after the failure must NOT be present
    assert "embed_batch" not in names


def test_harness_dispatches_isolated_extract(monkeypatch, tmp_path):
    _patch_stages(monkeypatch)
    fx = tmp_path / "x.pdf"
    fx.write_bytes(b"%PDF-1.4\nfake")

    harness = IngestHarness()
    prepared = PreparedInputs("", [], [], [])
    run = harness.run_scenario(extract_only(fx), prepared)
    assert run.error is None
    assert len(run.stages) == 1
    assert run.stages[0].name == "extract"


def test_harness_dispatches_isolated_chunk_with_prepared_text(monkeypatch, tmp_path):
    _patch_stages(monkeypatch)
    fx = tmp_path / "x.pdf"
    fx.write_bytes(b"%PDF-1.4\nfake")

    harness = IngestHarness()
    prepared = PreparedInputs(
        extracted_text="some prose here",
        sections=[],
        chunks=[],
        chunk_texts=[],
    )
    run = harness.run_scenario(chunk_only(fx), prepared)
    assert run.error is None
    assert run.stages[0].name == "chunk"
    assert run.stages[0].units["input_chars"] == len("some prose here")


def test_harness_dispatches_isolated_embed_batch_respects_batch_size(monkeypatch, tmp_path):
    _patch_stages(monkeypatch)
    fx = tmp_path / "x.pdf"
    fx.write_bytes(b"%PDF-1.4\nfake")

    harness = IngestHarness()
    prepared = PreparedInputs(
        extracted_text="",
        sections=[],
        chunks=[],
        chunk_texts=["c1", "c2", "c3", "c4", "c5"],
    )
    scenario = embed_batch(fx, batch_size=3)
    run = harness.run_scenario(scenario, prepared)
    assert run.stages[0].units["batch_size"] == 3


def test_harness_dispatches_isolated_entity_extract(monkeypatch, tmp_path):
    _patch_stages(monkeypatch)
    fx = tmp_path / "x.pdf"
    fx.write_bytes(b"%PDF-1.4\nfake")

    harness = IngestHarness()
    prepared = PreparedInputs(
        extracted_text="John Smith met Mary Doe in Warsaw on 2023-05-01.",
        sections=[],
        chunks=[],
        chunk_texts=[],
    )
    run = harness.run_scenario(entity_extract_only(fx), prepared)
    assert run.error is None
    assert run.stages[0].name == "entity_extract"


def test_harness_dispatches_section_detect(monkeypatch, tmp_path):
    _patch_stages(monkeypatch)
    fx = tmp_path / "x.pdf"
    fx.write_bytes(b"%PDF-1.4\nfake")

    harness = IngestHarness()
    prepared = PreparedInputs(
        extracted_text="1 Introduction\nbody\n2 Methods\nbody",
        sections=[],
        chunks=[],
        chunk_texts=[],
    )
    run = harness.run_scenario(section_detect_only(fx), prepared)
    assert run.error is None
    assert run.stages[0].name == "section_detect"


def test_harness_returns_error_when_prepared_missing_for_isolated_stage(tmp_path):
    """End-to-end can run without prepared inputs, isolated stages cannot."""
    fx = tmp_path / "x.pdf"
    fx.write_bytes(b"%PDF-1.4\nfake")

    harness = IngestHarness()
    run = harness.run_scenario(chunk_only(fx), prepared=None)
    assert run.error is not None
    assert "prepared" in run.error.lower()


def test_harness_dispatches_end_to_end_through_run_scenario(monkeypatch, tmp_path):
    """run_scenario(end_to_end) should produce the same output as run_end_to_end."""
    _patch_stages(monkeypatch)
    fx = tmp_path / "x.pdf"
    fx.write_bytes(b"%PDF-1.4\nfake")

    harness = IngestHarness()
    run = harness.run_scenario(end_to_end(fx), prepared=None)
    assert run.error is None
    assert len(run.stages) == 5
