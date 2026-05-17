import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import aiosqlite
import pytest

from config import get_settings
from models.database import init_database
from services.source_import.cloud_placeholders import detect_online_only_placeholder
from services.source_import.extractors import extract_business_document
from services.source_import.grants import clear_grants_for_tests
from services.source_import.manifest import mark_interrupted_batches
from services.source_import.scan import scan_folder
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
async def test_scan_reports_cloud_placeholder_without_reading_contents(client, tmp_path):
    (tmp_path / "proposal.docx").write_text("downloaded", encoding="utf-8")
    (tmp_path / ".proposal.docx.icloud").write_text("", encoding="utf-8")
    token = await _grant(client, tmp_path)

    with patch("builtins.open", side_effect=AssertionError("scan read file contents")):
        r = await client.post("/api/source-import/scan", json={"source_token": token})

    assert r.status_code == 200
    body = r.json()
    assert body["supported_file_count"] == 1
    assert body["skipped_file_count"] == 1
    assert body["skipped_by_reason"] == {"online_only_placeholder": 1}

    placeholder = next(
        row for row in body["files"] if row["relpath"] == ".proposal.docx.icloud"
    )
    assert placeholder["status"] == "skipped"
    assert placeholder["reason"] == "online_only_placeholder"
    assert placeholder["extension"] == ".docx"


@pytest.mark.anyio
async def test_cloud_placeholder_detector_uses_cloud_provider_xattrs(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "proposal.docx"
    path.write_text("metadata only", encoding="utf-8")
    monkeypatch.setattr(
        os,
        "listxattr",
        lambda _path, follow_symlinks=False: ["com.apple.fileprovider.fpfs#P"],
        raising=False,
    )
    monkeypatch.setattr(
        os,
        "getxattr",
        lambda _path, _name, follow_symlinks=False: b"",
        raising=False,
    )

    assert detect_online_only_placeholder(path) == "online_only_placeholder"


@pytest.mark.anyio
async def test_bundled_sample_dataset_scans_as_supported_demo_source():
    sample_root = (
        Path(__file__).parents[2]
        / "desktop"
        / "src-tauri"
        / "sample-data"
        / "deepfiles-demo-folder"
    )

    result = scan_folder(sample_root, scan_id="scan_sample")
    report = result.report
    relpaths = {item.relpath for item in result.files}

    assert report.source_display_name == "deepfiles-demo-folder"
    assert report.unsupported_file_count == 0
    assert report.supported_file_count == len(result.files)
    assert {
        ".csv",
        ".eml",
        ".html",
        ".md",
        ".rtf",
        ".zip",
    }.issubset(set(report.counts_by_extension))
    assert "Proposal - Northstar Pilot.md" in relpaths
    assert "Archive/handover-pack.zip" in relpaths

    archive_doc = extract_business_document(sample_root / "Archive" / "handover-pack.zip")
    assert archive_doc.source_type == "zip"
    assert "checklist.txt" in archive_doc.markdown


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

    completion = await client.get(f"/api/source-import/imports/{batch_id}/completion")
    assert completion.status_code == 200
    completion_body = completion.json()
    assert completion_body["batch_id"] == batch_id
    assert completion_body["imported_file_count"] == 1
    assert completion_body["duplicate_file_count"] == 1
    assert completion_body["can_ask_about_import"] is True
    assert completion_body["imported_extension_counts"] == {".md": 1}
    assert any(
        row["question"] == "Which files should I review first?"
        for row in completion_body["suggested_questions"]
    )

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

    from services.system_prompt import build_system_prompt_with_stats

    _prompt, stats = await build_system_prompt_with_stats(
        "What should I review first?",
        workspace_path=workspace,
        import_batch_id=batch_id,
    )
    note_relpaths = {
        note.relative_to(workspace / "memory").as_posix()
        for note in notes
    }
    assert "Only notes created by this import batch" in stats["retrieval_block"]
    assert any(
        item["via"] == "import_batch" and item["path"] in note_relpaths
        for item in stats["trace"]
    )


@pytest.mark.anyio
async def test_import_review_reports_skipped_and_failed_files(
    client,
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "workspace"
    (workspace / "memory").mkdir(parents=True)
    (workspace / "app").mkdir()
    await init_database(workspace / "app" / "jarvis.db")
    monkeypatch.setattr(get_settings(), "workspace_path", workspace)

    async def _fake_fast_ingest(
        _path,
        *,
        target_folder,
        workspace_path=None,
        original_name=None,
        **_kwargs,
    ):
        if original_name == "locked.md":
            raise RuntimeError("password_protected")
        name = Path(original_name or "imported.md").stem
        return {"path": f"{target_folder}/{name}.md", "total_notes": 1}

    monkeypatch.setattr("services.source_import.worker.fast_ingest", _fake_fast_ingest)
    monkeypatch.setattr("services.ingest_jobs.schedule_graph_rebuild", lambda **_kwargs: None)

    source = tmp_path / "Client A"
    source.mkdir()
    (source / "keep.md").write_text("# Keep\n\nSame body", encoding="utf-8")
    (source / "copy.md").write_text("# Keep\n\nSame body", encoding="utf-8")
    (source / "locked.md").write_text("# Locked\n\nNeeds password", encoding="utf-8")

    token = await _grant(client, source)
    scan = await client.post("/api/source-import/scan", json={"source_token": token})
    assert scan.status_code == 200
    selection = await client.post(
        f"/api/source-import/scans/{scan.json()['scan_id']}/selection",
        json={},
    )
    assert selection.status_code == 200
    started = await client.post(
        f"/api/source-import/scans/{scan.json()['scan_id']}/start",
        json={"selection_id": selection.json()["selection_id"]},
    )
    assert started.status_code == 200

    finished = await _wait_for_import(client, started.json()["batch_id"])
    assert finished["state"] == "completed"
    assert finished["imported_file_count"] == 1
    assert finished["skipped_file_count"] == 1
    assert finished["failed_file_count"] == 1

    review = await client.get(
        f"/api/source-import/imports/{started.json()['batch_id']}/review"
    )

    assert review.status_code == 200
    body = review.json()
    assert body["problem_file_count"] == 2
    assert body["skipped_file_count"] == 1
    assert body["failed_file_count"] == 1
    assert body["reason_counts"] == {
        "duplicate_content": 1,
        "password_protected": 1,
    }

    by_relpath = {item["relpath"]: item for item in body["files"]}
    assert by_relpath["locked.md"]["status"] == "failed"
    assert by_relpath["locked.md"]["can_retry"] is True
    assert by_relpath["locked.md"]["can_fix_locally"] is True
    assert by_relpath["copy.md"]["status"] == "skipped"
    assert by_relpath["copy.md"]["reason"] == "duplicate_content"
    assert by_relpath["copy.md"]["can_retry"] is False


@pytest.mark.anyio
async def test_import_skips_file_that_becomes_online_only_before_hash(
    client,
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "workspace"
    (workspace / "memory").mkdir(parents=True)
    (workspace / "app").mkdir()
    await init_database(workspace / "app" / "jarvis.db")
    monkeypatch.setattr(get_settings(), "workspace_path", workspace)
    monkeypatch.setattr("services.ingest_jobs.schedule_graph_rebuild", lambda **_kwargs: None)
    monkeypatch.setattr(
        "services.source_import.worker.detect_online_only_placeholder",
        lambda path: "online_only_placeholder" if path.name == "cloud.md" else None,
    )

    def _fail_hash(_path):
        raise AssertionError("hash read placeholder")

    monkeypatch.setattr("services.source_import.worker._sha256_file", _fail_hash)

    source = tmp_path / "Client A"
    source.mkdir()
    (source / "cloud.md").write_text("# Cloud\n\nNot local anymore", encoding="utf-8")

    token = await _grant(client, source)
    scan = await client.post("/api/source-import/scan", json={"source_token": token})
    assert scan.status_code == 200
    assert scan.json()["supported_file_count"] == 1
    selection = await client.post(
        f"/api/source-import/scans/{scan.json()['scan_id']}/selection",
        json={},
    )
    assert selection.status_code == 200

    started = await client.post(
        f"/api/source-import/scans/{scan.json()['scan_id']}/start",
        json={"selection_id": selection.json()["selection_id"]},
    )
    assert started.status_code == 200
    batch_id = started.json()["batch_id"]

    finished = await _wait_for_import(client, batch_id)
    assert finished["state"] == "completed"
    assert finished["imported_file_count"] == 0
    assert finished["skipped_file_count"] == 1
    assert finished["failed_file_count"] == 0
    assert finished["files"][0]["status"] == "skipped"
    assert finished["files"][0]["reason"] == "online_only_placeholder"

    review = await client.get(f"/api/source-import/imports/{batch_id}/review")
    assert review.status_code == 200
    body = review.json()
    assert body["reason_counts"] == {"online_only_placeholder": 1}
    assert body["files"][0]["can_fix_locally"] is True


@pytest.mark.anyio
async def test_cancel_import_stops_queued_files_after_current_file(
    client,
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "workspace"
    (workspace / "memory").mkdir(parents=True)
    (workspace / "app").mkdir()
    await init_database(workspace / "app" / "jarvis.db")
    monkeypatch.setattr(get_settings(), "workspace_path", workspace)

    first_ingest_started = asyncio.Event()
    release_first_ingest = asyncio.Event()
    ingest_calls = 0

    async def _fake_fast_ingest(
        _path,
        *,
        target_folder,
        workspace_path=None,
        original_name=None,
        **_kwargs,
    ):
        nonlocal ingest_calls
        ingest_calls += 1
        if ingest_calls == 1:
            first_ingest_started.set()
            await release_first_ingest.wait()
        name = Path(original_name or "imported.md").stem
        return {"path": f"{target_folder}/{name}.md", "total_notes": 1}

    monkeypatch.setattr("services.source_import.worker.fast_ingest", _fake_fast_ingest)

    source = tmp_path / "Client A"
    source.mkdir()
    for idx in range(3):
        (source / f"note-{idx}.md").write_text(f"# Note {idx}", encoding="utf-8")

    token = await _grant(client, source)
    scan = await client.post("/api/source-import/scan", json={"source_token": token})
    assert scan.status_code == 200
    selection = await client.post(
        f"/api/source-import/scans/{scan.json()['scan_id']}/selection",
        json={},
    )
    assert selection.status_code == 200

    started = await client.post(
        f"/api/source-import/scans/{scan.json()['scan_id']}/start",
        json={"selection_id": selection.json()["selection_id"]},
    )
    assert started.status_code == 200
    batch_id = started.json()["batch_id"]

    await asyncio.wait_for(first_ingest_started.wait(), timeout=2)
    cancelled = await client.post(f"/api/source-import/imports/{batch_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "cancelling"

    release_first_ingest.set()
    finished = await _wait_for_import(client, batch_id)

    assert finished["state"] == "cancelled"
    assert finished["imported_file_count"] == 1
    assert finished["skipped_file_count"] == 2
    assert ingest_calls == 1
    skipped_reasons = {
        item["reason"]
        for item in finished["files"]
        if item["status"] == "skipped"
    }
    assert skipped_reasons == {"cancelled_by_user"}


@pytest.mark.anyio
async def test_remove_import_archives_only_batch_notes_and_cleans_derived_rows(
    client,
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "workspace"
    (workspace / "memory").mkdir(parents=True)
    (workspace / "app").mkdir()
    db_path = workspace / "app" / "jarvis.db"
    await init_database(db_path)
    monkeypatch.setattr(get_settings(), "workspace_path", workspace)

    async def _fake_connect(*_args, **_kwargs):
        return SimpleNamespace(model_dump=lambda: {})

    monkeypatch.setattr("services.connection_service.connect_note", _fake_connect)
    monkeypatch.setattr("services.ingest_jobs.schedule_graph_rebuild", lambda **_kwargs: None)

    source = tmp_path / "Client A"
    source.mkdir()
    (source / "keep.md").write_text("# Keep\n\nBatch note", encoding="utf-8")

    token = await _grant(client, source)
    scan = await client.post("/api/source-import/scan", json={"source_token": token})
    assert scan.status_code == 200
    scan_body = scan.json()

    selection = await client.post(
        f"/api/source-import/scans/{scan_body['scan_id']}/selection",
        json={},
    )
    assert selection.status_code == 200

    started = await client.post(
        f"/api/source-import/scans/{scan_body['scan_id']}/start",
        json={"selection_id": selection.json()["selection_id"]},
    )
    assert started.status_code == 200
    batch_id = started.json()["batch_id"]

    finished = await _wait_for_import(client, batch_id)
    assert finished["state"] == "completed"
    note_path = next(
        path
        for item in finished["files"]
        for path in item["note_paths"]
    )
    imported_note = workspace / "memory" / note_path
    assert imported_note.exists()

    user_note = imported_note.parent / "user-added.md"
    user_note.write_text("# User added\n\nKeep this", encoding="utf-8")

    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO node_embeddings(
                node_id, node_type, label, embedding, content_hash,
                model_name, dimensions, embedded_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"note:{note_path}",
                "note",
                "Keep",
                b"0",
                "hash",
                "test-model",
                1,
                "2026-05-16T00:00:00Z",
            ),
        )
        await db.execute(
            """
            INSERT INTO enrichments(
                subject_type, subject_id, content_hash, model_id,
                prompt_version, status, payload, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "note",
                note_path,
                "hash",
                "test-model",
                1,
                "ok",
                "{}",
                "2026-05-16T00:00:00Z",
            ),
        )
        await db.execute(
            """
            INSERT OR REPLACE INTO dismissed_suggestions(
                note_path, target_path, dismissed_at
            )
            VALUES (?, ?, ?)
            """,
            (note_path, "knowledge/other.md", "2026-05-16T00:00:00Z"),
        )
        await db.commit()

    removed = await client.post(
        f"/api/source-import/imports/{batch_id}/remove",
        json={"confirm_batch_id": batch_id},
    )

    assert removed.status_code == 200
    removed_body = removed.json()
    assert removed_body["state"] == "removed"
    assert not imported_note.exists()
    assert (workspace / ".trash" / note_path).exists()
    assert user_note.exists()

    async with aiosqlite.connect(str(db_path)) as db:
        assert await _count_rows(db, "notes", "path", note_path) == 0
        assert await _count_rows(db, "node_embeddings", "node_id", f"note:{note_path}") == 0
        assert await _count_rows(db, "enrichments", "subject_id", note_path) == 0
        cursor = await db.execute(
            """
            SELECT COUNT(1) FROM dismissed_suggestions
            WHERE note_path = ? OR target_path = ?
            """,
            (note_path, note_path),
        )
        assert (await cursor.fetchone())[0] == 0

    second = await client.post(
        f"/api/source-import/imports/{batch_id}/remove",
        json={"confirm_batch_id": batch_id},
    )
    assert second.status_code == 200
    assert second.json()["state"] == "removed"


@pytest.mark.anyio
async def test_remove_import_failure_keeps_rows_for_unarchived_notes(
    client,
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "workspace"
    (workspace / "memory").mkdir(parents=True)
    (workspace / "app").mkdir()
    db_path = workspace / "app" / "jarvis.db"
    await init_database(db_path)
    monkeypatch.setattr(get_settings(), "workspace_path", workspace)

    async def _fake_connect(*_args, **_kwargs):
        return SimpleNamespace(model_dump=lambda: {})

    async def _fail_delete(*_args, **_kwargs):
        raise RuntimeError("archive failed")

    monkeypatch.setattr("services.connection_service.connect_note", _fake_connect)
    monkeypatch.setattr("services.ingest_jobs.schedule_graph_rebuild", lambda **_kwargs: None)

    source = tmp_path / "Client A"
    source.mkdir()
    (source / "keep.md").write_text("# Keep\n\nBatch note", encoding="utf-8")

    token = await _grant(client, source)
    scan = await client.post("/api/source-import/scan", json={"source_token": token})
    assert scan.status_code == 200
    selection = await client.post(
        f"/api/source-import/scans/{scan.json()['scan_id']}/selection",
        json={},
    )
    assert selection.status_code == 200
    started = await client.post(
        f"/api/source-import/scans/{scan.json()['scan_id']}/start",
        json={"selection_id": selection.json()["selection_id"]},
    )
    assert started.status_code == 200
    batch_id = started.json()["batch_id"]

    finished = await _wait_for_import(client, batch_id)
    assert finished["state"] == "completed"
    note_path = next(path for item in finished["files"] for path in item["note_paths"])
    imported_note = workspace / "memory" / note_path
    assert imported_note.exists()

    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO node_embeddings(
                node_id, node_type, label, embedding, content_hash,
                model_name, dimensions, embedded_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"note:{note_path}",
                "note",
                "Keep",
                b"0",
                "hash",
                "test-model",
                1,
                "2026-05-16T00:00:00Z",
            ),
        )
        await db.commit()

    monkeypatch.setattr("services.source_import.removal.delete_note", _fail_delete)

    removed = await client.post(
        f"/api/source-import/imports/{batch_id}/remove",
        json={"confirm_batch_id": batch_id},
    )

    assert removed.status_code == 409
    assert imported_note.exists()
    summary = await client.get(f"/api/source-import/imports/{batch_id}")
    assert summary.status_code == 200
    assert summary.json()["state"] == "completed"
    async with aiosqlite.connect(str(db_path)) as db:
        assert await _count_rows(db, "node_embeddings", "node_id", f"note:{note_path}") == 1


@pytest.mark.anyio
async def test_rescan_reports_changes_and_imports_only_changed_files(
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
    (source / "keep.md").write_text("# Keep\n\nSame", encoding="utf-8")
    (source / "change.md").write_text("# Change\n\nOld", encoding="utf-8")
    (source / "remove.md").write_text("# Remove\n\nGone soon", encoding="utf-8")
    for idx, name in enumerate(["keep.md", "change.md", "remove.md"], start=1):
        os.utime(source / name, (1_800_000_000 + idx, 1_800_000_000 + idx))

    token = await _grant(client, source)
    scan = await client.post("/api/source-import/scan", json={"source_token": token})
    assert scan.status_code == 200
    selection = await client.post(
        f"/api/source-import/scans/{scan.json()['scan_id']}/selection",
        json={},
    )
    assert selection.status_code == 200
    started = await client.post(
        f"/api/source-import/scans/{scan.json()['scan_id']}/start",
        json={"selection_id": selection.json()["selection_id"]},
    )
    assert started.status_code == 200
    batch_id = started.json()["batch_id"]
    finished = await _wait_for_import(client, batch_id)
    assert finished["state"] == "completed"
    assert finished["imported_file_count"] == 3

    (source / "change.md").write_text("# Change\n\nNew", encoding="utf-8")
    os.utime(source / "change.md", (1_900_000_000, 1_900_000_000))
    (source / "new.md").write_text("# New\n\nFresh", encoding="utf-8")
    os.utime(source / "new.md", (1_900_000_001, 1_900_000_001))
    (source / "remove.md").unlink()
    (source / "image.png").write_bytes(b"not supported")

    with patch("builtins.open", side_effect=AssertionError("rescan read file contents")):
        rescan = await client.post(f"/api/source-import/imports/{batch_id}/rescan")

    assert rescan.status_code == 200
    body = rescan.json()
    assert body["scan_id"].startswith("scan_")
    assert body["new_file_count"] == 1
    assert body["changed_file_count"] == 1
    assert body["unchanged_file_count"] == 1
    assert body["missing_file_count"] == 1
    assert body["unsupported_file_count"] == 1
    assert body["importable_file_count"] == 2

    by_relpath = {item["relpath"]: item for item in body["files"]}
    assert by_relpath["new.md"]["status"] == "new"
    assert by_relpath["change.md"]["status"] == "changed"
    assert by_relpath["keep.md"]["status"] == "unchanged"
    assert by_relpath["remove.md"]["status"] == "missing"
    assert by_relpath["image.png"]["status"] == "unsupported"

    rescan_selection = await client.post(
        f"/api/source-import/scans/{body['scan_id']}/selection",
        json={},
    )
    assert rescan_selection.status_code == 200
    rescan_selection_body = rescan_selection.json()
    assert rescan_selection_body["approved_file_count"] == 2
    assert {
        item["relpath"] for item in rescan_selection_body["approved_files"]
    } == {"change.md", "new.md"}

    started_changes = await client.post(
        f"/api/source-import/scans/{body['scan_id']}/start",
        json={"selection_id": rescan_selection_body["selection_id"]},
    )
    assert started_changes.status_code == 200
    changes_finished = await _wait_for_import(client, started_changes.json()["batch_id"])
    assert changes_finished["state"] == "completed"
    assert changes_finished["total_file_count"] == 2
    assert changes_finished["imported_file_count"] == 2
    assert {item["relpath"] for item in changes_finished["files"]} == {
        "change.md",
        "new.md",
    }


@pytest.mark.anyio
async def test_rescan_without_changes_has_no_import_scan(
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
    (source / "keep.md").write_text("# Keep\n\nSame", encoding="utf-8")
    os.utime(source / "keep.md", (1_800_000_000, 1_800_000_000))

    token = await _grant(client, source)
    scan = await client.post("/api/source-import/scan", json={"source_token": token})
    selection = await client.post(
        f"/api/source-import/scans/{scan.json()['scan_id']}/selection",
        json={},
    )
    started = await client.post(
        f"/api/source-import/scans/{scan.json()['scan_id']}/start",
        json={"selection_id": selection.json()["selection_id"]},
    )
    finished = await _wait_for_import(client, started.json()["batch_id"])
    assert finished["state"] == "completed"

    rescan = await client.post(
        f"/api/source-import/imports/{started.json()['batch_id']}/rescan"
    )

    assert rescan.status_code == 200
    body = rescan.json()
    assert body["scan_id"] is None
    assert body["importable_file_count"] == 0
    assert body["unchanged_file_count"] == 1
    assert body["files"][0]["status"] == "unchanged"


@pytest.mark.anyio
async def test_startup_recovery_marks_active_imports_interrupted(
    client,
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "workspace"
    (workspace / "memory").mkdir(parents=True)
    (workspace / "app").mkdir()
    db_path = workspace / "app" / "jarvis.db"
    await init_database(db_path)
    monkeypatch.setattr(get_settings(), "workspace_path", workspace)

    now = "2026-05-16T00:00:00Z"
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            """
            INSERT INTO source_import_batches(
                batch_id, scan_id, selection_id, source_kind, source_display_name,
                source_root_path, destination_root, state, total_file_count,
                total_bytes, created_note_count, current_file, started_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "import_active",
                "scan_1",
                "sel_1",
                "local_folder",
                "Client A",
                str(tmp_path / "Client A"),
                "imports/client-a",
                "importing",
                1,
                10,
                0,
                "note.md",
                now,
                now,
            ),
        )
        await db.execute(
            """
            INSERT INTO source_import_files(
                batch_id, file_id, relpath, filename, extension, size,
                modified_at, status, stage, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "import_active",
                "file_1",
                "note.md",
                "note.md",
                ".md",
                10,
                now,
                "importing",
                "reading",
                now,
            ),
        )
        await db.commit()

    assert await mark_interrupted_batches(workspace_path=workspace) == 1

    recovered = await client.get("/api/source-import/imports/import_active")
    assert recovered.status_code == 200
    body = recovered.json()
    assert body["state"] == "interrupted"
    assert body["current_file"] is None
    assert body["finished_at"] is not None
    assert body["files"][0]["status"] == "failed"
    assert body["files"][0]["stage"] == "interrupted"
    assert body["files"][0]["reason"] == "app_closed_during_import"


async def _count_rows(db: aiosqlite.Connection, table: str, column: str, value: str) -> int:
    cursor = await db.execute(
        f"SELECT COUNT(1) FROM {table} WHERE {column} = ?",
        (value,),
    )
    row = await cursor.fetchone()
    return int(row[0])
