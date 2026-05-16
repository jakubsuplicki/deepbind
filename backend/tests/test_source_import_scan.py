import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from config import get_settings
from models.database import init_database
from services.source_import.grants import clear_grants_for_tests
from services.source_import.store import clear_scans_for_tests
from utils.markdown import parse_frontmatter


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


async def _wait_for_import(client, batch_id: str) -> dict:
    terminal = {"completed", "failed", "cancelled", "interrupted", "removed"}
    for _ in range(80):
        r = await client.get(f"/api/source-import/imports/{batch_id}")
        assert r.status_code == 200
        body = r.json()
        if body["state"] in terminal:
            return body
        await asyncio.sleep(0.05)
    raise AssertionError("source import did not finish")


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


@pytest.mark.anyio
async def test_selection_applies_review_exclusions(client, tmp_path):
    source = tmp_path / "source"
    docs = source / "Docs"
    docs.mkdir(parents=True)
    (source / "keep.md").write_text("# Keep", encoding="utf-8")
    (source / "exclude-me.md").write_text("# Exclude", encoding="utf-8")
    (source / "notes.txt").write_text("notes", encoding="utf-8")
    (docs / "brief.md").write_text("# Brief", encoding="utf-8")
    (source / "image.png").write_bytes(b"not supported")

    token = await _grant(client, source)
    scan = await client.post("/api/source-import/scan", json={"source_token": token})
    assert scan.status_code == 200
    scan_body = scan.json()
    exclude_file_id = next(
        row["id"] for row in scan_body["files"] if row["relpath"] == "exclude-me.md"
    )

    selection = await client.post(
        f"/api/source-import/scans/{scan_body['scan_id']}/selection",
        json={
            "excluded_file_ids": [exclude_file_id],
            "excluded_extensions": [".txt"],
            "excluded_folders": ["Docs"],
        },
    )

    assert selection.status_code == 200
    body = selection.json()
    assert body["selection_id"].startswith("sel_")
    assert body["approved_file_count"] == 1
    assert body["approved_files"][0]["relpath"] == "keep.md"
    assert body["excluded_file_count"] == 3
    assert body["excluded_by_rule"] == {"file": 1, "file_type": 1, "folder": 1}
    assert body["unsupported_file_count"] == 1


@pytest.mark.anyio
async def test_selection_uses_full_scan_not_preview(client, tmp_path):
    for i in range(505):
        (tmp_path / f"note-{i:03}.md").write_text("# Note", encoding="utf-8")
    token = await _grant(client, tmp_path)

    scan = await client.post("/api/source-import/scan", json={"source_token": token})
    assert scan.status_code == 200
    scan_body = scan.json()
    assert scan_body["file_list_truncated"] is True
    assert len(scan_body["files"]) == 500

    selection = await client.post(
        f"/api/source-import/scans/{scan_body['scan_id']}/selection",
        json={},
    )

    assert selection.status_code == 200
    body = selection.json()
    assert body["approved_file_count"] == 505
    assert len(body["approved_files"]) == 500
    assert body["approved_file_list_truncated"] is True


@pytest.mark.anyio
async def test_scan_marks_business_document_types_supported(client, tmp_path):
    for name in [
        "proposal.docx",
        "pipeline.xlsx",
        "deck.pptx",
        "page.html",
        "memo.rtf",
        "message.eml",
        "archive.zip",
    ]:
        (tmp_path / name).write_text("placeholder", encoding="utf-8")
    token = await _grant(client, tmp_path)

    r = await client.post("/api/source-import/scan", json={"source_token": token})

    assert r.status_code == 200
    body = r.json()
    assert body["supported_file_count"] == 7
    assert body["unsupported_file_count"] == 0
    assert set(body["counts_by_extension"]) >= {
        ".docx",
        ".xlsx",
        ".pptx",
        ".html",
        ".rtf",
        ".eml",
        ".zip",
    }


@pytest.mark.anyio
async def test_start_import_processes_approved_files_with_safe_provenance(
    client,
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "workspace"
    (workspace / "memory").mkdir(parents=True)
    (workspace / "app").mkdir()
    await init_database(workspace / "app" / "jarvis.db")
    monkeypatch.setattr(get_settings(), "workspace_path", workspace)

    async def _fake_connect(*_args, **_kwargs):
        return SimpleNamespace(model_dump=lambda: {})

    monkeypatch.setattr("services.connection_service.connect_note", _fake_connect)
    monkeypatch.setattr("services.ingest_jobs.schedule_graph_rebuild", lambda **_kwargs: None)

    source = tmp_path / "Client A"
    source.mkdir()
    (source / "keep.md").write_text("# Keep\n\nSame body", encoding="utf-8")
    (source / "duplicate.md").write_text("# Keep\n\nSame body", encoding="utf-8")
    (source / "exclude.txt").write_text("Do not import", encoding="utf-8")
    (source / "image.png").write_bytes(b"unsupported")

    token = await _grant(client, source)
    scan = await client.post("/api/source-import/scan", json={"source_token": token})
    assert scan.status_code == 200
    scan_body = scan.json()

    selection = await client.post(
        f"/api/source-import/scans/{scan_body['scan_id']}/selection",
        json={"excluded_extensions": [".txt"]},
    )
    assert selection.status_code == 200
    selection_body = selection.json()
    assert selection_body["approved_file_count"] == 2

    started = await client.post(
        f"/api/source-import/scans/{scan_body['scan_id']}/start",
        json={"selection_id": selection_body["selection_id"]},
    )
    assert started.status_code == 200
    batch_id = started.json()["batch_id"]

    finished = await _wait_for_import(client, batch_id)
    assert finished["state"] == "completed"
    assert finished["total_file_count"] == 2
    assert finished["imported_file_count"] == 1
    assert finished["skipped_file_count"] == 1
    assert finished["failed_file_count"] == 0
    assert finished["created_note_count"] == 1
    assert {
        item["reason"]
        for item in finished["files"]
        if item["status"] == "skipped"
    } == {"duplicate_content"}

    notes = list((workspace / "memory").rglob("*.md"))
    assert len(notes) == 1
    content = notes[0].read_text(encoding="utf-8")
    fm, _body = parse_frontmatter(content)
    assert fm["source_kind"] == "local_folder_import"
    assert fm["source_relpath"] in {"keep.md", "duplicate.md"}
    assert fm["source"] in {"keep.md", "duplicate.md"}
    assert fm["import_batch_id"] == batch_id
    assert str(source) not in content
    assert not list((workspace / "memory" / "imports").glob("**/exclude.md"))
