"""Ingest scenario definitions (ADR 013).

A scenario picks a fixture file + which pipeline stage to time. Two
shapes:

- **end-to-end** — runs every stage in sequence and reports per-stage
  durations plus total wall clock. Captures the user-visible "drop a
  PDF and wait" experience.
- **stage-isolated** — runs only one stage (extract / section_detect /
  chunk / embed_batch / entity_extract) on inputs prepared once per
  scenario. Lets the knob loop iterate on a single stage without
  re-paying the cost of upstream stages each timed run.

Stage-isolated scenarios share fixture-derived inputs across timed runs;
the harness prepares those inputs in a one-shot setup pass before the
warm-up + N timed runs begin. This mirrors the "warm a model first" /
"discard the cold run" discipline used by the chat-latency harness.

Fixtures live under ``samples/`` at the repo root. The default fixture
is ``samples/911Report.pdf`` (~7.5 MB, 585 pages) — large enough that
embed-batch dominates the end-to-end timing on M5 Pro 24 GB and the
breakdown is interpretable.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional


# ── Stages ──────────────────────────────────────────────────────────────────


class Stage(str, Enum):
    """Pipeline stages we time individually.

    String values are stable, used as JSON keys in baseline output and
    in scenario names. ``END_TO_END`` is the composite scenario; the
    other values name an individual stage exercised in isolation.
    """

    END_TO_END = "end_to_end"
    EXTRACT = "extract"
    SECTION_DETECT = "section_detect"
    CHUNK = "chunk"
    EMBED_BATCH = "embed_batch"
    ENTITY_EXTRACT = "entity_extract"


# Stage names emitted in end-to-end per-stage breakdown. Ordered by
# pipeline execution; the runner zips this with the timing list to
# produce stable JSON regardless of dict iteration order.
END_TO_END_STAGE_ORDER: tuple[str, ...] = (
    Stage.EXTRACT.value,
    Stage.SECTION_DETECT.value,
    Stage.CHUNK.value,
    Stage.EMBED_BATCH.value,
    Stage.ENTITY_EXTRACT.value,
)


# ── Scenario ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class IngestScenario:
    """One benchmarking unit.

    ``fixture_path`` is absolute; the CLI resolves project-relative paths
    at parse time so a baseline JSON committed by the M5 dev box is
    interpretable on any machine that has the same fixture present.

    ``embed_batch_size`` controls how many chunks are fed into the
    embedder for the EMBED_BATCH and END_TO_END stages. ``0`` means
    "embed every chunk" (matches production); a positive value caps
    the batch — used by the isolated EMBED_BATCH-N scenario for
    per-batch throughput measurement. Ignored for other stages.
    """

    name: str
    stage: Stage
    fixture_path: Path
    description: str
    embed_batch_size: int = 0


# ── v1 scenario set ─────────────────────────────────────────────────────────


DEFAULT_FIXTURE_RELATIVE = Path("samples/911Report.pdf")
"""Project-relative path to the canonical large-PDF fixture."""


def end_to_end(fixture: Path) -> IngestScenario:
    """Full pipeline, single timed run reports per-stage durations."""
    return IngestScenario(
        name=f"end-to-end-{fixture.stem}",
        stage=Stage.END_TO_END,
        fixture_path=fixture,
        description=(
            "Full ingest pipeline against the fixture (extract → sections → "
            "chunk → embed → entities). Reports per-stage breakdown plus "
            "total wall clock. Mirrors the user-visible 'drop a PDF' path."
        ),
    )


def extract_only(fixture: Path) -> IngestScenario:
    """PDF text extraction only (no downstream stages).

    Uses whatever extractor :func:`services.ingest._extract_pdf_text`
    is wired to — currently pypdfium2 (ADR 013 knob-1, 2026-04-28).
    """
    return IngestScenario(
        name=f"extract-{fixture.stem}",
        stage=Stage.EXTRACT,
        fixture_path=fixture,
        description=(
            "PDF text extraction in isolation. Bounded only by I/O + "
            "page-by-page CPU work; embedding/NER never run."
        ),
    )


def section_detect_only(fixture: Path) -> IngestScenario:
    """Heading-based section detection on pre-extracted text."""
    return IngestScenario(
        name=f"section-detect-{fixture.stem}",
        stage=Stage.SECTION_DETECT,
        fixture_path=fixture,
        description=(
            "Heading-detection heuristic on pre-extracted PDF text. "
            "Pure-Python; the input is cached across timed runs."
        ),
    )


def chunk_only(fixture: Path) -> IngestScenario:
    """Markdown chunking on pre-extracted text concatenated as one body."""
    return IngestScenario(
        name=f"chunk-{fixture.stem}",
        stage=Stage.CHUNK,
        fixture_path=fixture,
        description=(
            "chunk_markdown on the concatenated extracted text. Pure-"
            "Python multi-granularity chunker; input cached across runs."
        ),
    )


def embed_batch(fixture: Path, batch_size: int = 64) -> IngestScenario:
    """fastembed batch encode on pre-prepared chunks."""
    return IngestScenario(
        name=f"embed-batch-{batch_size}-{fixture.stem}",
        stage=Stage.EMBED_BATCH,
        fixture_path=fixture,
        description=(
            f"fastembed batch encode of {batch_size} chunks. Chunks are "
            f"prepared once per scenario (chunk_markdown on extracted "
            f"text); each timed run re-runs encode on the same inputs."
        ),
        embed_batch_size=batch_size,
    )


def entity_extract_only(fixture: Path) -> IngestScenario:
    """Entity extraction (spaCy NER + regex) on pre-extracted text."""
    return IngestScenario(
        name=f"entity-extract-{fixture.stem}",
        stage=Stage.ENTITY_EXTRACT,
        fixture_path=fixture,
        description=(
            "Entity extraction (spaCy NER for persons/orgs + regex for "
            "dates/projects) on the first ~50 KB of extracted text. The "
            "production path runs this per-section, so the per-call shape "
            "is what matters here, not the full-document total."
        ),
    )


def default_scenarios(fixture: Path) -> list[IngestScenario]:
    """v1 default scenario set against a single fixture.

    End-to-end gives the headline number; the five isolated stage
    scenarios let the knob loop iterate without re-paying upstream cost.
    """
    return [
        end_to_end(fixture),
        extract_only(fixture),
        section_detect_only(fixture),
        chunk_only(fixture),
        embed_batch(fixture, batch_size=64),
        entity_extract_only(fixture),
    ]
