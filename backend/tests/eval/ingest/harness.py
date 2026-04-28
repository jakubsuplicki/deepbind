"""Per-stage timing harness for the ingest pipeline (ADR 013).

Two responsibilities:

1. **Stage primitives** — thin wrappers around the production functions
   (``services.ingest._extract_pdf_text``, ``services.ingest._detect_pdf_sections``,
   ``services.chunking.chunk_markdown``, ``services.embedding_service.embed_texts``,
   ``services.entity_extraction.extract_entities``) that return a
   :class:`StageTiming` alongside the stage's output.

2. **Scenario dispatcher** — :meth:`IngestHarness.run_scenario` accepts a
   prepared :class:`PreparedInputs` (the same upstream data used across
   N timed runs of an isolated-stage scenario) and runs the target
   stage once, returning a :class:`IngestRun`.

This module deliberately does NOT call ``fast_ingest`` for end-to-end
because that path also writes notes / indexes / runs Smart Connect — none
of which we want in the latency number. The end-to-end run here is the
*compute-only* path: extract → sections → chunk → embed → entities. The
disk-write + indexing cost is a separate concern (it's bounded by SQLite
and rarely the bottleneck on consumer hardware).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .scenarios import END_TO_END_STAGE_ORDER, IngestScenario, Stage


# ── Result types ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StageTiming:
    """One stage's wall-clock duration plus shape metadata.

    ``units`` carries the size descriptors that make the duration
    interpretable: how many pages were extracted, how many chars were
    chunked, how many chunks were embedded, etc. Persisted into the
    baseline JSON so a future regression diff reads "embed went from
    400 → 600 ms on 4096 chunks" instead of "embed got slower."
    """

    name: str
    duration_ms: float
    units: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IngestRun:
    """One scenario × seed timed run.

    For isolated-stage scenarios ``stages`` has one entry (the target
    stage). For end-to-end ``stages`` has one per pipeline stage in
    :data:`END_TO_END_STAGE_ORDER`.

    ``error`` is set on stage failure; the runner reports the error and
    moves on (one OOM in section-detect shouldn't kill the whole grid).
    """

    scenario_name: str
    fixture_path: str
    total_ms: float
    stages: tuple[StageTiming, ...]
    error: Optional[str] = None


# ── Prepared inputs (shared across timed runs) ─────────────────────────────


@dataclass
class PreparedInputs:
    """Upstream-stage outputs cached once per scenario.

    Built by :func:`prepare_inputs` in the runner. Stage-isolated
    scenarios consume only the field they care about; end-to-end
    rebuilds everything fresh each run (so its measured durations
    include the upstream cost).
    """

    extracted_text: str
    sections: list  # list of services.ingest._DocumentSection
    chunks: list  # list of services.chunking.Chunk
    chunk_texts: list[str]


# ── Stage primitives ────────────────────────────────────────────────────────


def _now_ms() -> float:
    return time.perf_counter() * 1000.0


def time_extract(pdf_path: Path) -> tuple[str, StageTiming]:
    """Run PDF text extraction; return (text, timing).

    Uses the production extractor (``services.ingest._extract_pdf_text``)
    so harness numbers reflect whatever library the production path is
    currently wired to (ADR 013 knob-1: pypdfium2 since 2026-04-28).
    """
    from services.ingest import _extract_pdf_text

    t0 = _now_ms()
    text = _extract_pdf_text(pdf_path)
    duration = _now_ms() - t0
    page_count = text.count("\n\n") + 1 if text else 0  # extract joins pages with \n\n
    return text, StageTiming(
        name=Stage.EXTRACT.value,
        duration_ms=duration,
        units={
            "pages_approx": page_count,
            "chars": len(text),
            "fixture_bytes": pdf_path.stat().st_size if pdf_path.exists() else 0,
        },
    )


def time_section_detect(text: str) -> tuple[list, StageTiming]:
    """Heading-detection on pre-extracted text; return (sections, timing)."""
    from services.ingest import _detect_pdf_sections

    t0 = _now_ms()
    sections = _detect_pdf_sections(text)
    duration = _now_ms() - t0
    return sections, StageTiming(
        name=Stage.SECTION_DETECT.value,
        duration_ms=duration,
        units={
            "input_chars": len(text),
            "sections_detected": len(sections),
        },
    )


def time_chunk(text: str) -> tuple[list, StageTiming]:
    """Run chunk_markdown on the concatenated text; return (chunks, timing).

    The ingest path normally calls chunk_markdown per *note* (one per
    section after split). For benchmarking we run it on the whole body
    once; the chunker's complexity is dominated by total char count, not
    by section boundaries, so the per-char ms is the metric that
    matters.
    """
    from services.chunking import chunk_markdown

    # chunk_markdown expects markdown including frontmatter; pass the body
    # as-is (no frontmatter) — the parser handles missing frontmatter
    # by returning empty fm dict + body verbatim.
    t0 = _now_ms()
    chunks = chunk_markdown(text)
    duration = _now_ms() - t0
    return chunks, StageTiming(
        name=Stage.CHUNK.value,
        duration_ms=duration,
        units={
            "input_chars": len(text),
            "chunks_emitted": len(chunks),
        },
    )


def time_embed_batch(
    chunk_texts: list[str], batch_size: int
) -> tuple[list, StageTiming]:
    """Run fastembed encode on chunks; return (embeddings, timing).

    The embedding model is lazy-loaded on first call (~3-4s cold start,
    ~400 MB). The runner discards the cold run via ``n_warmup_runs=1``
    so the timed numbers reflect the warm-cache cost.
    """
    from services.embedding_service import embed_texts

    sample = chunk_texts[:batch_size] if batch_size else chunk_texts
    t0 = _now_ms()
    embeddings = embed_texts(sample)
    duration = _now_ms() - t0
    return embeddings, StageTiming(
        name=Stage.EMBED_BATCH.value,
        duration_ms=duration,
        units={
            "batch_size": len(sample),
            "input_chars_total": sum(len(s) for s in sample),
            "vector_dim": len(embeddings[0]) if embeddings else 0,
        },
    )


def time_entity_extract(text: str, *, max_chars: int = 50_000) -> tuple[list, StageTiming]:
    """Run entity extraction; return (entities, timing).

    The production path runs this per-section, so the per-call shape on
    a section-sized window is the realistic measurement. ``max_chars``
    truncates the input — the 911 Report is several MB of text, and
    spaCy's runtime is roughly linear, so timing the full document
    would dominate everything else and obscure per-section behavior.
    """
    from services.entity_extraction import extract_entities

    sample = text[:max_chars]
    t0 = _now_ms()
    entities = extract_entities(sample)
    duration = _now_ms() - t0
    return entities, StageTiming(
        name=Stage.ENTITY_EXTRACT.value,
        duration_ms=duration,
        units={
            "input_chars": len(sample),
            "entities_extracted": len(entities),
        },
    )


# ── End-to-end runner ──────────────────────────────────────────────────────


def run_end_to_end(fixture: Path, *, embed_batch_size: int = 0) -> IngestRun:
    """Run every stage in pipeline order, capture per-stage durations.

    Each stage's input is the previous stage's output, which is what the
    production path does. The total wall clock is the sum of stage
    durations (no harness overhead between stages).

    ``embed_batch_size`` bounds the number of chunks fed into the
    embedder. Default 0 = embed every chunk (matches production's
    ``embed_note_chunks`` which embeds all chunks of a note in one
    ``aembed_texts`` call). A non-zero value caps the count for
    isolated per-batch-throughput scenarios.

    History: until 2026-04-28 this defaulted to 64 to keep nightly
    runs short, but that produced misleading end-to-end numbers — a
    5,000-chunk document was reported as ~950 ms when production
    actually takes ~41 seconds. Honest measurement is worth the extra
    minute per run.
    """
    t0 = _now_ms()
    timings: list[StageTiming] = []
    error: Optional[str] = None
    try:
        text, t_extract = time_extract(fixture)
        timings.append(t_extract)

        sections, t_sections = time_section_detect(text)
        timings.append(t_sections)

        chunks, t_chunk = time_chunk(text)
        timings.append(t_chunk)

        chunk_texts = [c.text for c in chunks]
        _emb, t_embed = time_embed_batch(chunk_texts, embed_batch_size)
        timings.append(t_embed)

        _ents, t_entity = time_entity_extract(text)
        timings.append(t_entity)
    except Exception as exc:  # noqa: BLE001 — surface every error uniformly
        error = f"{type(exc).__name__}: {exc}"

    total_ms = _now_ms() - t0
    # Ensure stage order matches END_TO_END_STAGE_ORDER for stable JSON
    by_name = {t.name: t for t in timings}
    ordered = tuple(
        by_name[name]
        for name in END_TO_END_STAGE_ORDER
        if name in by_name
    )
    return IngestRun(
        scenario_name=f"end-to-end-{fixture.stem}",
        fixture_path=str(fixture),
        total_ms=total_ms,
        stages=ordered,
        error=error,
    )


# ── Scenario dispatcher ─────────────────────────────────────────────────────


@dataclass
class IngestHarness:
    """Dispatch a :class:`IngestScenario` to the right stage primitive.

    The harness is stateless across scenarios but expects
    :class:`PreparedInputs` for stage-isolated runs. The runner builds
    these once per scenario via :func:`prepare_inputs` and reuses them
    across the warm-up + N timed runs.
    """

    def run_scenario(
        self,
        scenario: IngestScenario,
        prepared: Optional[PreparedInputs],
    ) -> IngestRun:
        """Run the scenario's target stage once; return one :class:`IngestRun`."""
        if scenario.stage is Stage.END_TO_END:
            return run_end_to_end(
                scenario.fixture_path,
                embed_batch_size=scenario.embed_batch_size,
            )

        if prepared is None:
            return IngestRun(
                scenario_name=scenario.name,
                fixture_path=str(scenario.fixture_path),
                total_ms=0.0,
                stages=(),
                error=f"prepared inputs missing for stage-isolated scenario {scenario.name!r}",
            )

        t0 = _now_ms()
        try:
            timing = self._dispatch(scenario, prepared)
        except Exception as exc:  # noqa: BLE001
            return IngestRun(
                scenario_name=scenario.name,
                fixture_path=str(scenario.fixture_path),
                total_ms=_now_ms() - t0,
                stages=(),
                error=f"{type(exc).__name__}: {exc}",
            )
        return IngestRun(
            scenario_name=scenario.name,
            fixture_path=str(scenario.fixture_path),
            total_ms=_now_ms() - t0,
            stages=(timing,),
        )

    def _dispatch(
        self, scenario: IngestScenario, prepared: PreparedInputs
    ) -> StageTiming:
        if scenario.stage is Stage.EXTRACT:
            _text, timing = time_extract(scenario.fixture_path)
            return timing
        if scenario.stage is Stage.SECTION_DETECT:
            _sections, timing = time_section_detect(prepared.extracted_text)
            return timing
        if scenario.stage is Stage.CHUNK:
            _chunks, timing = time_chunk(prepared.extracted_text)
            return timing
        if scenario.stage is Stage.EMBED_BATCH:
            _embs, timing = time_embed_batch(
                prepared.chunk_texts, scenario.embed_batch_size
            )
            return timing
        if scenario.stage is Stage.ENTITY_EXTRACT:
            _ents, timing = time_entity_extract(prepared.extracted_text)
            return timing
        raise ValueError(f"unknown stage {scenario.stage!r}")
