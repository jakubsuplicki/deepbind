"""Tests for ADR 014 — desktop bundle excludes cloud providers.

Covers both build targets — desktop bundle (`JARVIS_DESKTOP_BUNDLE=1`,
the v1 default) and cloud SKU (`JARVIS_DESKTOP_BUNDLE=0`) — so the
cloud-provider code path stays correct enough to ship per ADR §139
"CI must include both build targets to keep the path correct enough to
ship."

Verifies:

  1. ``services/bundle.py`` — `is_desktop_bundle()` reads the env var on
     every call (test fixtures monkeypatch the var per-test).
  2. The api-keys router is NOT registered in the desktop bundle (route
     returns 404) but IS registered in the cloud SKU.
  3. The chat WS handler emits a structured `local-only` error when the
     user requests a non-Ollama provider in the desktop bundle.
  4. ``/api/bundle/capabilities`` always returns a valid payload — the
     audit signal is the response shape, not the endpoint's
     presence/absence.
"""

from __future__ import annotations

import importlib
import os
from unittest.mock import MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.testclient import TestClient

pytestmark = pytest.mark.anyio(backends=["asyncio"])


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


def _fresh_app(*, desktop_bundle: bool):
    """Build a fresh FastAPI app honouring the `JARVIS_DESKTOP_BUNDLE` flag.

    The default `app` instance imported by conftest.py is locked in at
    import time with `JARVIS_DESKTOP_BUNDLE=1` (the v1 default). To test
    the cloud-SKU surface we need a separate app whose router-include
    decisions are made under the flipped flag — so we set the env var and
    call `create_app()` directly. No module reload required since
    `create_app` reads the flag at call time.
    """
    flag_value = "1" if desktop_bundle else "0"
    with patch.dict(os.environ, {"JARVIS_DESKTOP_BUNDLE": flag_value}):
        from main import create_app
        return create_app()


# ── services/bundle.py helpers ──────────────────────────────────────────────


class TestBundleHelpers:
    def test_is_desktop_bundle_default_true(self):
        from services.bundle import is_desktop_bundle
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("JARVIS_DESKTOP_BUNDLE", None)
            assert is_desktop_bundle() is True

    def test_is_desktop_bundle_explicit_zero(self):
        from services.bundle import is_desktop_bundle
        with patch.dict(os.environ, {"JARVIS_DESKTOP_BUNDLE": "0"}):
            assert is_desktop_bundle() is False

    def test_is_desktop_bundle_explicit_one(self):
        from services.bundle import is_desktop_bundle
        with patch.dict(os.environ, {"JARVIS_DESKTOP_BUNDLE": "1"}):
            assert is_desktop_bundle() is True

    def test_bundle_capabilities_desktop(self):
        from services.bundle import bundle_capabilities
        with patch.dict(os.environ, {"JARVIS_DESKTOP_BUNDLE": "1"}):
            caps = bundle_capabilities()
        assert "local-llm" in caps
        assert "vault-markdown" in caps
        assert "knowledge-graph" in caps
        assert "semantic-search" in caps
        assert "cloud-llm" not in caps
        assert "api-keys" not in caps
        assert "external-providers" not in caps

    def test_bundle_capabilities_cloud_sku(self):
        from services.bundle import bundle_capabilities
        with patch.dict(os.environ, {"JARVIS_DESKTOP_BUNDLE": "0"}):
            caps = bundle_capabilities()
        assert "cloud-llm" in caps
        assert "api-keys" in caps
        assert "external-providers" in caps
        assert "local-llm" in caps  # local stays unconditional


# ── /api/bundle/capabilities probe endpoint ────────────────────────────────


class TestBundleCapabilitiesEndpoint:
    @pytest.mark.anyio
    async def test_returns_capability_array(self, client):
        resp = await client.get("/api/bundle/capabilities")
        assert resp.status_code == 200
        data = resp.json()
        assert "capabilities" in data
        assert isinstance(data["capabilities"], list)
        assert "is_desktop_bundle" in data
        assert "cloud_providers_available" in data

    @pytest.mark.anyio
    async def test_local_llm_always_present(self, client):
        resp = await client.get("/api/bundle/capabilities")
        assert "local-llm" in resp.json()["capabilities"]

    @pytest.mark.anyio
    async def test_cloud_capabilities_match_flag(self, client):
        resp = await client.get("/api/bundle/capabilities")
        data = resp.json()
        if data["is_desktop_bundle"]:
            assert "cloud-llm" not in data["capabilities"]
            assert "external-providers" not in data["capabilities"]
        else:
            assert "cloud-llm" in data["capabilities"]


# ── api-keys router gating ─────────────────────────────────────────────────


def test_api_keys_route_404_in_desktop_bundle():
    """Desktop bundle (`JARVIS_DESKTOP_BUNDLE=1`) — route does not exist."""
    app = _fresh_app(desktop_bundle=True)
    with TestClient(app) as client:
        resp = client.patch("/api/settings/api-key", json={"api_key": "sk-x"})
    assert resp.status_code == 404


def test_api_keys_route_registered_in_cloud_sku():
    """Cloud SKU (`JARVIS_DESKTOP_BUNDLE=0`) — route exists, no-op handler."""
    app = _fresh_app(desktop_bundle=False)
    with TestClient(app) as client:
        resp = client.patch("/api/settings/api-key", json={"api_key": "sk-real-key"})
    assert resp.status_code == 200
    assert resp.json()["api_key_set"] is True


def test_api_keys_rejects_empty_in_cloud_sku():
    """Symmetric — empty key is 422 in the cloud SKU (existing no-op contract)."""
    app = _fresh_app(desktop_bundle=False)
    with TestClient(app) as client:
        resp = client.patch("/api/settings/api-key", json={"api_key": ""})
    assert resp.status_code == 422


# ── chat-WS cloud-503 guard ─────────────────────────────────────────────────


def test_chat_ws_emits_local_only_error_for_cloud_provider_when_excluded(tmp_path, monkeypatch):
    """When `cloud_providers_available()` is False (the desktop bundle
    state) and the user requests a non-Ollama provider, the WS chat
    handler emits a structured error with `bundle_capability=local-only`
    and closes — same audit-shape contract as the HTTP probe path."""
    # Workspace isolation — minimal stand-in for test_chat_ws.py's fixture
    settings = MagicMock()
    settings.workspace_path = tmp_path
    for mod in [
        "services.session_service", "services.memory_service",
        "services.graph_service", "services.context_builder",
        "services.preference_service", "services.token_tracking",
        "services.workspace_service",
    ]:
        try:
            monkeypatch.setattr(f"{mod}.get_settings", lambda: settings)
        except AttributeError:
            pass
    for d in [
        "app", "app/sessions", "memory", "memory/inbox",
        "memory/preferences", "graph",
    ]:
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    import sqlite3
    from models.database import FTS_SQL, SCHEMA_SQL, TRIGGER_SQL
    with sqlite3.connect(str(tmp_path / "app" / "jarvis.db")) as conn:
        conn.executescript(SCHEMA_SQL + FTS_SQL + TRIGGER_SQL)

    from main import app
    with patch("services.bundle.cloud_providers_available", return_value=False), \
         patch("routers.chat.get_api_key", return_value="sk-ant-test-key"):
        with TestClient(app) as client:
            with client.websocket_connect("/api/chat/ws") as ws:
                ws.receive_json()  # session_start
                ws.send_json({"content": "hi", "provider": "anthropic"})
                events = []
                while True:
                    msg = ws.receive_json()
                    events.append(msg)
                    if msg["type"] == "done":
                        break

    errors = [e for e in events if e["type"] == "error"]
    assert len(errors) == 1
    assert errors[0].get("bundle_capability") == "local-only"
    assert "ADR 014" in errors[0]["content"] or "cloud" in errors[0]["content"].lower()


def test_chat_ws_passes_through_when_cloud_available(tmp_path, monkeypatch):
    """When cloud SDKs ARE available, the cloud-503 guard does not fire —
    the chat dispatch proceeds normally."""
    from services.claude import StreamEvent

    settings = MagicMock()
    settings.workspace_path = tmp_path
    for mod in [
        "services.session_service", "services.memory_service",
        "services.graph_service", "services.context_builder",
        "services.preference_service", "services.token_tracking",
        "services.workspace_service",
    ]:
        try:
            monkeypatch.setattr(f"{mod}.get_settings", lambda: settings)
        except AttributeError:
            pass
    for d in [
        "app", "app/sessions", "memory", "memory/inbox",
        "memory/preferences", "graph",
    ]:
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    import sqlite3
    from models.database import FTS_SQL, SCHEMA_SQL, TRIGGER_SQL
    with sqlite3.connect(str(tmp_path / "app" / "jarvis.db")) as conn:
        conn.executescript(SCHEMA_SQL + FTS_SQL + TRIGGER_SQL)

    async def _gen(**kwargs):
        yield StreamEvent(type="text_delta", content="OK")

    from main import app
    instance = MagicMock()
    instance.stream_response = _gen
    with patch("services.bundle.cloud_providers_available", return_value=True), \
         patch("routers.chat.get_api_key", return_value="sk-ant-test-key"), \
         patch("routers.chat._make_llm", return_value=instance):
        with TestClient(app) as client:
            with client.websocket_connect("/api/chat/ws") as ws:
                ws.receive_json()
                ws.send_json({"content": "hi", "provider": "anthropic"})
                events = []
                while True:
                    msg = ws.receive_json()
                    events.append(msg)
                    if msg["type"] == "done":
                        break

    errors = [e for e in events if e["type"] == "error"]
    assert all(e.get("bundle_capability") != "local-only" for e in errors)
    text = [e for e in events if e["type"] == "text_delta"]
    assert any("OK" in e["content"] for e in text)
