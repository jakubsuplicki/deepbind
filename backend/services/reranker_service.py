"""Local cross-encoder reranker.

Uses fastembed's TextCrossEncoder to re-score retrieval candidates with a
cross-encoder. 100% local, no API calls — same trust model as
embedding_service.

Per ADR 018 (v1 ships English-only), the bundled model is
``BAAI/bge-reranker-v2-m3`` via the ``onnx-community/bge-reranker-v2-m3-ONNX``
INT8 quantized port (~570 MB on disk, Apache-2.0). Replaces the previous
``jinaai/jina-reranker-v2-base-multilingual`` which was CC-BY-NC-4.0 and
therefore not commercially redistributable. The English-first re-research
(`docs/research/models/reranker-english-first.md`) re-confirmed v2-m3 as
the pick because it still beats every smaller English-only alternative on
BEIR (~51.8 vs ~49.5 for `bge-reranker-large`, ~49.6 for `jina-v1-turbo-en`,
~46.9 for `bge-reranker-base`).

Override via ``JARVIS_RERANKER_MODEL`` env var. Disable entirely via
``JARVIS_DISABLE_RERANKER=1``. There is no runtime-download fallback: a
bundled .app trusts its bundle and degrades to fusion-only retrieval
(returns ``None``) if the model fails to load — the audit explicitly
rejected the previous fallback path because it'd defeat ADR 003 §A's
offline-first contract by silently fetching a different model from HF
when the primary failed.

The reranker is a *cross-encoder*: it scores each (query, document) pair
together, which is far more precise than the bi-encoder cosine score —
but slower (~50-150 ms per query for 20 candidates on CPU). We use it
only for the final precision pass over the top-N hybrid candidates.

**Score distribution.** bge-reranker-v2-m3 returns *unbounded logits*
(typical range −8 to +5), unlike Jina v2 which returned sigmoid-normalized
[0,1]. The retrieval pipeline at services/retrieval/pipeline.py applies
min-max normalization within the rerank pool before fusion blending, so
the unbounded range is handled at that layer — there is no consumer-side
threshold that assumes [0,1] absolute values. If a future caller adds
threshold-based gating, it must use the pool-normalized score (the
``_signals["rerank"]`` field) rather than the raw ``_rerank`` value.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# --- Model configuration ----------------------------------------------------
# Use the onnx-community INT8 ONNX port of BAAI/bge-reranker-v2-m3. The
# upstream BAAI repo ships PyTorch weights only; the onnx-community fork
# (Xenova-maintained) provides FP32/FP16/INT8/UINT8/Q8/BNB4/Q4 ONNX
# variants. INT8 is the deployment target (570 MB on disk, ~100-150 ms
# top-20 on Apple Silicon CPU). Apache-2.0 throughout the chain
# (BAAI/bge-m3 base → BAAI/bge-reranker-v2-m3 fine-tune → onnx-community
# port — none introduce custom or non-commercial terms).
DEFAULT_MODEL = "onnx-community/bge-reranker-v2-m3-ONNX"
DEFAULT_MODEL_FILE = "onnx/model_int8.onnx"

# Singleton model instance (lazy-loaded)
_model = None
_model_name: Optional[str] = None
_custom_model_registered = False


def is_disabled() -> bool:
    """Honour user opt-out."""
    return os.environ.get("JARVIS_DISABLE_RERANKER") == "1"


def _resolve_model_name() -> str:
    return os.environ.get("JARVIS_RERANKER_MODEL", DEFAULT_MODEL)


def _bundled_cache_dir() -> Optional[str]:
    """Locate the bundled fastembed cache when running inside PyInstaller.

    Mirrors embedding_service._bundled_cache_dir — both models share the
    same `_bundled_models/fastembed/` directory at runtime. When not frozen
    (dev / pytest), return None and let fastembed use its default ~/.cache.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return None
    candidate = Path(meipass) / "_bundled_models" / "fastembed"
    return str(candidate) if candidate.is_dir() else None


def _register_custom_model() -> None:
    """Register the onnx-community port with fastembed's TextCrossEncoder.

    fastembed 0.8.0 doesn't know about `onnx-community/bge-reranker-v2-m3-ONNX`
    in its built-in registry (open issue qdrant/fastembed#494). The
    `add_custom_model` API is the supported path for ONNX cross-encoders
    that aren't yet in the registry. Idempotent — guarded by the module-
    level flag so repeated calls don't error.
    """
    global _custom_model_registered
    if _custom_model_registered:
        return
    from fastembed.rerank.cross_encoder import TextCrossEncoder
    from fastembed.common.model_description import ModelSource

    TextCrossEncoder.add_custom_model(
        model=DEFAULT_MODEL,
        model_file=DEFAULT_MODEL_FILE,
        sources=ModelSource(hf=DEFAULT_MODEL),
        description=(
            "BAAI/bge-reranker-v2-m3 (Apache-2.0), INT8 ONNX via the "
            "onnx-community port. See ADR 018 + "
            "docs/research/models/reranker-english-first.md."
        ),
        license="apache-2.0",
        size_in_gb=0.6,
    )
    _custom_model_registered = True


def _get_model():
    """Lazy-load reranker on first use. Returns None if unavailable.

    No runtime-download fallback: the bundled .app trusts its bundle. If
    the model fails to load we degrade to fusion-only retrieval (the
    pipeline handles None gracefully) rather than fetching an alternative
    model from HuggingFace at runtime, which would defeat the offline-
    first contract.
    """
    global _model, _model_name
    if _model is not None:
        return _model
    if is_disabled():
        return None

    try:
        from fastembed.rerank.cross_encoder import TextCrossEncoder
    except ImportError:
        logger.info("fastembed reranker unavailable (TextCrossEncoder not importable)")
        return None

    try:
        _register_custom_model()
    except Exception as exc:
        logger.warning("Reranker custom-model registration failed: %s", exc)
        return None

    name = _resolve_model_name()
    cache_dir = _bundled_cache_dir()
    try:
        if cache_dir:
            logger.info("Loading reranker model %s from bundled cache %s ...", name, cache_dir)
            _model = TextCrossEncoder(model_name=name, cache_dir=cache_dir)
        else:
            logger.info("Loading reranker model %s (default cache) ...", name)
            _model = TextCrossEncoder(model_name=name)
        _model_name = name
        logger.info("Reranker model loaded.")
        return _model
    except Exception as exc:
        logger.warning("Reranker model %s failed to load: %s", name, exc)
        _model = None
        return None


def is_available() -> bool:
    """Cheap probe — does NOT load the model unless already loaded."""
    if is_disabled():
        return False
    try:
        from fastembed.rerank.cross_encoder import TextCrossEncoder  # noqa: F401
    except ImportError:
        return False
    return True


def model_name() -> Optional[str]:
    """Return the active reranker model name (loads if needed)."""
    _get_model()
    return _model_name


def rerank(
    query: str,
    documents: List[str],
) -> Optional[List[float]]:
    """Score each document against *query* with the cross-encoder.

    Returns a list of float scores aligned with *documents*, or ``None``
    if the reranker is unavailable (caller should fall back to original
    ordering). Higher score = more relevant. Scores are unbounded logits
    (see module docstring); callers that need [0,1] should apply min-max
    normalization within their pool, as services/retrieval/pipeline.py does.
    """
    if not query or not documents:
        return None
    model = _get_model()
    if model is None:
        return None
    try:
        scores = list(model.rerank(query, documents))
        return [float(s) for s in scores]
    except Exception as exc:
        logger.warning("Reranker scoring failed: %s", exc)
        return None


def rerank_pairs(
    query: str,
    items: List[Tuple[str, str]],
) -> Optional[List[Tuple[str, float]]]:
    """Convenience: rerank a list of ``(id, document_text)`` pairs.

    Returns a list of ``(id, score)`` sorted by descending score, or
    ``None`` if the reranker is unavailable.
    """
    if not items:
        return []
    docs = [text for _, text in items]
    scores = rerank(query, docs)
    if scores is None:
        return None
    paired = [(item_id, score) for (item_id, _), score in zip(items, scores)]
    paired.sort(key=lambda x: x[1], reverse=True)
    return paired
