"""Minimal PIL.Image stub registered on the frozen entrypoint.

fastembed's package __init__ eagerly imports the image-embedding code path
(fastembed.image.transform.functional uses ``Image.Resampling.BILINEAR``
as a default kwarg at module top), even when the consumer only uses
TextEmbedding. Including the real Pillow in the desktop bundle adds ~30 MB
of native libs (libjpeg/libtiff/libwebp/...) and triggers a pathological
amfid scan on first launch — `_dyld_start` hangs indefinitely for ad-hoc-
signed bundles on macOS Tahoe (verified 2026-04-29 during G2b graduation).

We never call image embedding code; the image-path imports only need
``Image.Image`` (a class), ``Image.Resampling`` (an IntEnum-like with the
sampler names), ``Image.fromarray`` / ``Image.new`` / ``Image.open``
(callables) to *resolve* during import. They never run. This stub
satisfies the import so PIL can stay in PyInstaller's `excludes` list.

`install()` is called from backend/scripts/run_frozen.py before any fastembed
import. In dev (non-frozen) we don't install — Pillow is not in the venv either,
and dev never exercises the bundle's frozen-only fastembed code paths.
"""

from __future__ import annotations

import enum
import sys
import types


def install() -> None:
    """Register PIL + PIL.Image as no-op modules in sys.modules.

    Idempotent: if PIL is already imported (real Pillow), do nothing. This way
    a future change that pulls Pillow back in won't be silently overridden.
    """
    if "PIL" in sys.modules:
        return

    pil_module = types.ModuleType("PIL")
    image_module = types.ModuleType("PIL.Image")

    class _Image:
        """Placeholder for PIL.Image.Image — only used as a type alias."""

    class _Resampling(enum.IntEnum):
        # Names + values per Pillow's Image.Resampling enum. Values are not
        # exercised at runtime in the bundle, but match upstream so any
        # accidental equality check still works.
        NEAREST = 0
        LANCZOS = 1
        BILINEAR = 2
        BICUBIC = 3
        BOX = 4
        HAMMING = 5

    def _stub_callable(*_args, **_kwargs):
        raise RuntimeError(
            "PIL.Image stub: image embedding is not supported in this build "
            "(see backend/utils/pil_stub.py)."
        )

    image_module.Image = _Image
    image_module.Resampling = _Resampling
    image_module.fromarray = _stub_callable
    image_module.new = _stub_callable
    image_module.open = _stub_callable

    pil_module.Image = image_module

    sys.modules["PIL"] = pil_module
    sys.modules["PIL.Image"] = image_module
