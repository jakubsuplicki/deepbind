"""ADR 014 — desktop-bundle build-flag helpers.

Single source of truth for `JARVIS_DESKTOP_BUNDLE`. Read once at import
(env var lookups are cheap but caching avoids per-request `os.environ`
hits in hot paths) and exposed via three helpers:

  * ``is_desktop_bundle()`` — True when the running process is the
    structurally cloud-excluded desktop bundle (default: True per ADR §A).
  * ``cloud_providers_available()`` — True when the cloud-provider SDKs
    (`anthropic` / `openai` / `google.generativeai`) are importable. False
    in the desktop bundle (excluded by PyInstaller spec) or when running
    a dev environment without those packages installed.
  * ``bundle_capabilities()`` — list of capability strings shipped in the
    `Info.plist` audit array; `"local-llm"` always present, `"cloud-llm"`
    + `"api-keys"` + `"external-providers"` only when the bundle includes
    cloud support.

The flag default is `1` — the v1 product *is* the desktop bundle (per
ADR 014 §A). Dev mode that wants the cloud-provider test surface sets
``JARVIS_DESKTOP_BUNDLE=0`` explicitly.
"""

from __future__ import annotations

import importlib.util
import logging
import os
from typing import List

logger = logging.getLogger(__name__)


_DESKTOP_BUNDLE_ENV = "JARVIS_DESKTOP_BUNDLE"


def is_desktop_bundle() -> bool:
    """Read the build flag. Default ``True`` per ADR 014 §A.

    Read on every call (not cached) so test fixtures that monkeypatch the
    env var via ``os.environ`` see the change without restarting the
    process. The cost is negligible (env-var dict lookup).
    """
    return os.environ.get(_DESKTOP_BUNDLE_ENV, "1") == "1"


def cloud_providers_available() -> bool:
    """Probe whether the cloud-provider SDKs are importable.

    Used by the chat router to decide whether to return a 503 with the
    `X-Bundle-Capability: local-only` header for non-Ollama provider
    requests. The check uses ``importlib.util.find_spec`` — no actual
    import side-effects, just the module-resolution probe.
    """
    for name in ("anthropic", "openai"):
        try:
            if importlib.util.find_spec(name) is None:
                return False
        except (ImportError, ValueError):  # pragma: no cover — env-dependent
            return False
    return True


def bundle_capabilities() -> List[str]:
    """Return the capability list written into ``Info.plist`` (ADR §90).

    Order is stable: a buyer's audit script can pin against the array
    contents in any order, but we keep insertion order deterministic for
    diff readability.
    """
    caps: List[str] = ["local-llm", "vault-markdown", "knowledge-graph", "semantic-search"]
    if not is_desktop_bundle():
        caps += ["cloud-llm", "api-keys", "external-providers"]
    return caps
