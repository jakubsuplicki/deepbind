import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.anyio(backends=["asyncio"])

from models.database import init_database
from services.ingest import IngestError, fast_ingest


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "knowledge").mkdir()
    (tmp_path / "memory" / "inbox").mkdir()
    (tmp_path / "app").mkdir()
    return tmp_path


@pytest.fixture
async def ws_db(ws):
    await init_database(ws / "app" / "jarvis.db")
    return ws


@pytest.fixture
def md_file(tmp_path):
    f = tmp_path / "source" / "test.md"
    f.parent.mkdir(exist_ok=True)
    f.write_text("---\ntitle: Original\ntags: [test]\n---\n\nHello markdown content.")
    return f


@pytest.fixture
def txt_file(tmp_path):
    f = tmp_path / "source" / "notes.txt"
    f.parent.mkdir(exist_ok=True)
    f.write_text("This is plain text content.\nSecond line.")
    return f


@pytest.fixture
def empty_file(tmp_path):
    f = tmp_path / "source" / "empty.md"
    f.parent.mkdir(exist_ok=True)
    f.write_text("")
    return f


@pytest.fixture
def large_file(tmp_path):
    f = tmp_path / "source" / "large.txt"
    f.parent.mkdir(exist_ok=True)
    f.write_text("A" * 1_100_000)
    return f


@pytest.mark.anyio
async def test_import_markdown_copies_file(ws_db, md_file):
    result = await fast_ingest(md_file, "knowledge", workspace_path=ws_db)
    target = ws_db / "memory" / result["path"]
    assert target.exists()


@pytest.mark.anyio
async def test_import_markdown_indexed(ws_db, md_file):
    result = await fast_ingest(md_file, "knowledge", workspace_path=ws_db)
    assert result["folder"] == "knowledge"
    assert result["path"].endswith(".md")


@pytest.mark.anyio
async def test_import_markdown_preserves_frontmatter(ws_db, md_file):
    result = await fast_ingest(md_file, "knowledge", workspace_path=ws_db)
    target = ws_db / "memory" / result["path"]
    content = target.read_text()
    assert "title: Original" in content


@pytest.mark.anyio
async def test_import_txt_converts_to_md(ws_db, txt_file):
    result = await fast_ingest(txt_file, "knowledge", workspace_path=ws_db)
    assert result["path"].endswith(".md")


@pytest.mark.anyio
async def test_import_txt_content_preserved(ws_db, txt_file):
    result = await fast_ingest(txt_file, "knowledge", workspace_path=ws_db)
    target = ws_db / "memory" / result["path"]
    content = target.read_text()
    assert "plain text content" in content
    assert "Second line" in content


@pytest.mark.anyio
async def test_import_pdf_extracts_text(ws_db, tmp_path):
    """PDF ingest requires pypdfium2. Skip if not installed."""
    try:
        import pypdfium2  # noqa: F401
    except ImportError:
        pytest.skip("pypdfium2 not installed")

    # Create a minimal PDF for test would require a library
    # Instead we just test the error case
    fake_pdf = tmp_path / "source" / "test.pdf"
    fake_pdf.parent.mkdir(exist_ok=True)
    fake_pdf.write_bytes(b"%PDF-1.4 fake")
    with pytest.raises(Exception):
        await fast_ingest(fake_pdf, "knowledge", workspace_path=ws_db)


@pytest.mark.anyio
async def test_import_pdf_indexed(ws_db, tmp_path):
    """Test placeholder; real test requires a real PDF fixture."""
    pytest.skip("PDF ingest test requires actual PDF fixture")


@pytest.mark.anyio
async def test_import_json_pretty_prints(ws_db, tmp_path):
    src = tmp_path / "source" / "config.json"
    src.parent.mkdir(exist_ok=True)
    src.write_text('{"name":"jarvis","items":[1,2,3],"meta":{"k":"\u00f3"}}', encoding="utf-8")

    result = await fast_ingest(src, "knowledge", workspace_path=ws_db)
    assert result["path"].endswith(".md")
    target = ws_db / "memory" / result["path"]
    content = target.read_text(encoding="utf-8")
    assert "```json" in content
    # Pretty-printed (indented) and unicode preserved
    assert '"name": "jarvis"' in content
    assert "\u00f3" in content


@pytest.mark.anyio
async def test_import_invalid_json_keeps_raw_text(ws_db, tmp_path):
    src = tmp_path / "source" / "broken.json"
    src.parent.mkdir(exist_ok=True)
    src.write_text("{ not valid json", encoding="utf-8")

    result = await fast_ingest(src, "knowledge", workspace_path=ws_db)
    target = ws_db / "memory" / result["path"]
    content = target.read_text(encoding="utf-8")
    assert "```json" in content
    assert "not valid json" in content


@pytest.mark.anyio
async def test_import_duplicate_path_renames(ws_db, md_file):
    await fast_ingest(md_file, "knowledge", workspace_path=ws_db)
    result2 = await fast_ingest(md_file, "knowledge", workspace_path=ws_db)
    assert result2["path"] != "knowledge/test.md"
    assert "-1" in result2["path"] or "-2" in result2["path"]


@pytest.mark.anyio
async def test_import_empty_file_handled(ws_db, empty_file):
    result = await fast_ingest(empty_file, "knowledge", workspace_path=ws_db)
    assert result["path"].endswith(".md")


@pytest.mark.anyio
async def test_import_large_file_handled(ws_db, large_file):
    result = await fast_ingest(large_file, "knowledge", workspace_path=ws_db)
    assert result["size"] > 100_000


def _mock_ollama_response(text: str):
    """Build a minimal `ChatResponse`-like mock that satisfies smart_enrich."""
    from unittest.mock import MagicMock
    resp = MagicMock()
    resp.message = MagicMock(content=text)
    return resp


@pytest.mark.anyio
async def test_enrich_adds_summary(ws_db, md_file):
    """Smart enrich now dispatches against the local Ollama runtime (ADR 015)."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from services.ingest import smart_enrich

    result = await fast_ingest(md_file, "knowledge", workspace_path=ws_db)

    mock_client = MagicMock()
    mock_client.chat = AsyncMock(return_value=_mock_ollama_response('{"summary": "A test note.", "tags": ["test", "example"]}'))
    mock_client.close = AsyncMock()

    with patch("ollama.AsyncClient", return_value=mock_client):
        enrich_result = await smart_enrich(result["path"], "", workspace_path=ws_db)
        assert enrich_result["summary"] == "A test note."
        assert enrich_result["enriched"] is True


@pytest.mark.anyio
async def test_enrich_adds_tags(ws_db, md_file):
    from unittest.mock import AsyncMock, MagicMock, patch
    from services.ingest import smart_enrich

    result = await fast_ingest(md_file, "knowledge", workspace_path=ws_db)

    mock_client = MagicMock()
    mock_client.chat = AsyncMock(return_value=_mock_ollama_response('{"summary": "Test", "tags": ["newtag"]}'))
    mock_client.close = AsyncMock()

    with patch("ollama.AsyncClient", return_value=mock_client):
        enrich_result = await smart_enrich(result["path"], "", workspace_path=ws_db)
        assert "newtag" in enrich_result["tags"]


@pytest.mark.anyio
async def test_enrich_preserves_existing_frontmatter(ws_db, md_file):
    from unittest.mock import AsyncMock, MagicMock, patch
    from services.ingest import smart_enrich

    result = await fast_ingest(md_file, "knowledge", workspace_path=ws_db)

    mock_client = MagicMock()
    mock_client.chat = AsyncMock(return_value=_mock_ollama_response('{"summary": "Sum", "tags": ["extra"]}'))
    mock_client.close = AsyncMock()

    with patch("ollama.AsyncClient", return_value=mock_client):
        await smart_enrich(result["path"], "", workspace_path=ws_db)

    target = ws_db / "memory" / result["path"]
    content = target.read_text()
    assert "title:" in content


@pytest.mark.anyio
async def test_enrich_calls_ollama(ws_db, md_file):
    """Renamed from test_enrich_uses_claude — ADR 015: dispatch is local."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from services.ingest import smart_enrich

    result = await fast_ingest(md_file, "knowledge", workspace_path=ws_db)

    mock_client = MagicMock()
    mock_client.chat = AsyncMock(return_value=_mock_ollama_response('{"summary": "x", "tags": []}'))
    mock_client.close = AsyncMock()

    with patch("ollama.AsyncClient", return_value=mock_client):
        await smart_enrich(result["path"], "", workspace_path=ws_db)
        mock_client.chat.assert_called_once()
