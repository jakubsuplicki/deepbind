from pathlib import Path
from unittest.mock import patch

import pytest

from services.source_import.grants import clear_grants_for_tests
from services.source_import.store import clear_scans_for_tests


pytestmark = pytest.mark.anyio(backends=["asyncio"])


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


@pytest.fixture(autouse=True)
def _clear_source_import_state(monkeypatch):
    monkeypatch.setenv("JARVIS_SOURCE_IMPORT_GRANT_TOKEN", "test-shell-token")
    clear_grants_for_tests()
    clear_scans_for_tests()
    yield
    clear_grants_for_tests()
    clear_scans_for_tests()


async def _grant(client, path: Path) -> str:
    r = await client.post(
        "/api/source-import/grants",
        json={"path": str(path), "source_kind": "local_folder"},
        headers={"x-deepfiles-shell-token": "test-shell-token"},
    )
    assert r.status_code == 200
    return r.json()["source_token"]


@pytest.mark.anyio
async def test_grant_rejects_missing_shell_token(client, tmp_path):
    r = await client.post(
        "/api/source-import/grants",
        json={"path": str(tmp_path), "source_kind": "local_folder"},
    )
    assert r.status_code == 403


@pytest.mark.anyio
async def test_scan_rejects_unauthorized_or_expired_token(client):
    r = await client.post(
        "/api/source-import/scan",
        json={"source_token": "not-a-real-source-token"},
    )
    assert r.status_code == 400
    assert "grant" in r.json()["detail"].lower()


@pytest.mark.anyio
async def test_metadata_scan_reports_counts_without_reading_contents(client, tmp_path):
    source = tmp_path / "Client A"
    source.mkdir()
    (source / "brief.md").write_text("# Brief\n\nhello", encoding="utf-8")
    (source / "data.json").write_text('{"ok": true}', encoding="utf-8")
    (source / "image.png").write_bytes(b"not imported")
    hidden = source / ".git"
    hidden.mkdir()
    (hidden / "config").write_text("secret", encoding="utf-8")

    token = await _grant(client, source)

    with patch("builtins.open", side_effect=AssertionError("scan read file contents")):
        r = await client.post(
            "/api/source-import/scan",
            json={"source_token": token},
        )

    assert r.status_code == 200
    body = r.json()
    assert body["source_display_name"] == "Client A"
    assert body["proposed_destination_root"] == "memory/imports/client-a/"
    assert body["supported_file_count"] == 2
    assert body["unsupported_file_count"] == 1
    assert body["skipped_file_count"] == 1
    assert body["skipped_by_reason"]["hidden_or_system_folder"] == 1
    assert body["counts_by_extension"] == {".json": 1, ".md": 1, ".png": 1}
    assert {row["relpath"] for row in body["files"]} == {
        "brief.md",
        "data.json",
        "image.png",
    }
    assert next(row for row in body["files"] if row["relpath"] == "brief.md")["status"] == "supported"
    assert next(row for row in body["files"] if row["relpath"] == "image.png")["reason"] == "unsupported_file_type"

    scan_id = body["scan_id"]
    r2 = await client.get(f"/api/source-import/scans/{scan_id}")
    assert r2.status_code == 200
    assert r2.json()["scan_id"] == scan_id


@pytest.mark.anyio
async def test_scan_token_is_single_use(client, tmp_path):
    (tmp_path / "note.md").write_text("# Note", encoding="utf-8")
    token = await _grant(client, tmp_path)

    first = await client.post("/api/source-import/scan", json={"source_token": token})
    assert first.status_code == 200

    second = await client.post("/api/source-import/scan", json={"source_token": token})
    assert second.status_code == 400


@pytest.mark.anyio
async def test_scan_reports_file_limit(client, tmp_path):
    for i in range(3):
        (tmp_path / f"note-{i}.md").write_text("# Note", encoding="utf-8")
    token = await _grant(client, tmp_path)

    r = await client.post(
        "/api/source-import/scan",
        json={"source_token": token, "max_files": 2},
    )

    assert r.status_code == 200
    body = r.json()
    assert body["limit_hit"] is True
    assert body["total_files_seen"] == 3
    assert body["supported_file_count"] == 2
    assert body["skipped_by_reason"]["scan_file_limit"] == 1


@pytest.mark.anyio
async def test_scan_skips_symlink_outside_root(client, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    source = tmp_path / "source"
    source.mkdir()
    (source / "inside.md").write_text("# Inside", encoding="utf-8")
    link = source / "outside-link.txt"
    try:
        link.symlink_to(outside)
    except (NotImplementedError, OSError):
        pytest.skip("symlinks unavailable on this platform")

    token = await _grant(client, source)
    r = await client.post("/api/source-import/scan", json={"source_token": token})

    assert r.status_code == 200
    body = r.json()
    assert body["supported_file_count"] == 1
    assert body["skipped_by_reason"]["symlink_outside_root"] == 1
