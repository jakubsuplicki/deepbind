import json
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def ws_path(tmp_path):
    return tmp_path / "Jarvis"


@pytest.mark.anyio
async def test_get_status_no_workspace(client, ws_path):
    with patch("services.workspace_service.get_settings") as mock_s:
        mock_s.return_value.workspace_path = ws_path
        response = await client.get("/api/workspace/status")
    assert response.status_code == 200
    assert response.json()["initialized"] is False


@pytest.mark.anyio
async def test_post_init_creates_workspace(client, ws_path):
    with patch("services.workspace_service.get_settings") as mock_s:
        mock_s.return_value.workspace_path = ws_path
        response = await client.post("/api/workspace/init", json={})
    assert response.status_code == 201
    assert (ws_path / "app" / "config.json").exists()


@pytest.mark.anyio
async def test_post_init_returns_structure(client, ws_path):
    with patch("services.workspace_service.get_settings") as mock_s:
        mock_s.return_value.workspace_path = ws_path
        response = await client.post("/api/workspace/init", json={})
    data = response.json()
    assert data["status"] == "ok"
    assert "workspace_path" in data


@pytest.mark.anyio
async def test_get_status_after_init(client, ws_path):
    with patch("services.workspace_service.get_settings") as mock_s:
        mock_s.return_value.workspace_path = ws_path
        await client.post("/api/workspace/init", json={})
        response = await client.get("/api/workspace/status")
    assert response.status_code == 200
    data = response.json()
    assert data["initialized"] is True
    # ADR 015 — local-only build does not surface api_key_set / key_storage
    # on the workspace status response. The fields don't exist; the bundle
    # has no cloud-provider code paths to gate.
    assert "api_key_set" not in data
    assert "key_storage" not in data


@pytest.mark.anyio
async def test_post_init_duplicate(client, ws_path):
    with patch("services.workspace_service.get_settings") as mock_s:
        mock_s.return_value.workspace_path = ws_path
        await client.post("/api/workspace/init", json={})
        response = await client.post("/api/workspace/init", json={})
    assert response.status_code == 409


@pytest.mark.anyio
async def test_workspace_config_has_no_api_key_fields(client, ws_path):
    """ADR 015 — config.json never carries api_key_set / key_storage."""
    with patch("services.workspace_service.get_settings") as mock_s:
        mock_s.return_value.workspace_path = ws_path
        await client.post("/api/workspace/init", json={})
    config = json.loads((ws_path / "app" / "config.json").read_text())
    assert "api_key_set" not in config
    assert "key_storage" not in config


@pytest.mark.anyio
async def test_api_key_not_in_any_response(client, ws_path):
    """No API key string appears in any HTTP response body."""
    key = "sk-ant-secret-key-99999999"
    with patch("services.workspace_service.get_settings") as mock_s:
        mock_s.return_value.workspace_path = ws_path
        r1 = await client.post("/api/workspace/init", json={})
        r2 = await client.get("/api/workspace/status")
    assert key not in r1.text
    assert key not in r2.text

