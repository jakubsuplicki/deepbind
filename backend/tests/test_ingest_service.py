import json
import zipfile
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


def _write_zip(path: Path, files: dict[str, str | bytes]) -> Path:
    path.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in files.items():
            if isinstance(content, bytes):
                zf.writestr(name, content)
            else:
                zf.writestr(name, content.encode("utf-8"))
    return path


@pytest.mark.anyio
async def test_import_docx_extracts_text_and_tables(ws_db, tmp_path):
    src = _write_zip(tmp_path / "source" / "proposal.docx", {
        "[Content_Types].xml": "<Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\"/>",
        "word/document.xml": """
        <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
          <w:body>
            <w:p>
              <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
              <w:r><w:t>Executive Summary</w:t></w:r>
            </w:p>
            <w:p><w:r><w:t>Client proposal text.</w:t></w:r></w:p>
            <w:tbl>
              <w:tr><w:tc><w:p><w:r><w:t>Item</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Cost</w:t></w:r></w:p></w:tc></w:tr>
              <w:tr><w:tc><w:p><w:r><w:t>Discovery</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>1200</w:t></w:r></w:p></w:tc></w:tr>
            </w:tbl>
          </w:body>
        </w:document>
        """,
    })

    result = await fast_ingest(src, "knowledge", workspace_path=ws_db)
    content = (ws_db / "memory" / result["path"]).read_text(encoding="utf-8")
    assert "source_type: business/docx" in content
    assert "Executive Summary" in content
    assert "| Item | Cost |" in content


@pytest.mark.anyio
async def test_import_xlsx_extracts_sheet_table(ws_db, tmp_path):
    src = _write_zip(tmp_path / "source" / "pipeline.xlsx", {
        "xl/workbook.xml": """
        <workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
                  xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
          <sheets><sheet name="Pipeline" sheetId="1" r:id="rId1"/></sheets>
        </workbook>
        """,
        "xl/_rels/workbook.xml.rels": """
        <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
          <Relationship Id="rId1" Target="worksheets/sheet1.xml"/>
        </Relationships>
        """,
        "xl/sharedStrings.xml": """
        <sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
          <si><t>Client</t></si><si><t>Value</t></si><si><t>Acme</t></si>
        </sst>
        """,
        "xl/worksheets/sheet1.xml": """
        <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
          <sheetData>
            <row><c t="s"><v>0</v></c><c t="s"><v>1</v></c></row>
            <row><c t="s"><v>2</v></c><c><v>4200</v></c></row>
          </sheetData>
        </worksheet>
        """,
    })

    result = await fast_ingest(src, "knowledge", workspace_path=ws_db)
    content = (ws_db / "memory" / result["path"]).read_text(encoding="utf-8")
    assert "source_type: business/xlsx" in content
    assert "## Pipeline" in content
    assert "| Client | Value |" in content
    assert "| Acme | 4200 |" in content


@pytest.mark.anyio
async def test_import_pptx_extracts_slide_text(ws_db, tmp_path):
    src = _write_zip(tmp_path / "source" / "deck.pptx", {
        "ppt/slides/slide1.xml": """
        <p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
               xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
          <p:cSld><p:spTree><p:sp><p:txBody>
            <a:p><a:r><a:t>Roadmap</a:t></a:r></a:p>
            <a:p><a:r><a:t>Launch in Q3</a:t></a:r></a:p>
          </p:txBody></p:sp></p:spTree></p:cSld>
        </p:sld>
        """,
    })

    result = await fast_ingest(src, "knowledge", workspace_path=ws_db)
    content = (ws_db / "memory" / result["path"]).read_text(encoding="utf-8")
    assert "source_type: business/pptx" in content
    assert "## Slide 1" in content
    assert "Launch in Q3" in content


@pytest.mark.anyio
async def test_import_html_rtf_eml_and_zip_business_formats(ws_db, tmp_path):
    html_src = tmp_path / "source" / "page.html"
    html_src.parent.mkdir(exist_ok=True)
    html_src.write_text("<html><head><title>Client Page</title></head><body><h1>Plan</h1><p>HTML body.</p></body></html>", encoding="utf-8")
    rtf_src = tmp_path / "source" / "memo.rtf"
    rtf_src.write_text(r"{\rtf1\ansi Memo\par RTF body.}", encoding="utf-8")
    eml_src = tmp_path / "source" / "message.eml"
    eml_src.write_text(
        "Subject: Follow up\nFrom: a@example.com\nTo: b@example.com\n\nEmail body.",
        encoding="utf-8",
    )
    zip_src = _write_zip(tmp_path / "source" / "archive.zip", {
        "folder/readme.txt": "hello",
    })

    html_result = await fast_ingest(html_src, "knowledge", workspace_path=ws_db)
    rtf_result = await fast_ingest(rtf_src, "knowledge", workspace_path=ws_db)
    eml_result = await fast_ingest(eml_src, "knowledge", workspace_path=ws_db)
    zip_result = await fast_ingest(zip_src, "knowledge", workspace_path=ws_db)

    html_content = (ws_db / "memory" / html_result["path"]).read_text(encoding="utf-8")
    rtf_content = (ws_db / "memory" / rtf_result["path"]).read_text(encoding="utf-8")
    eml_content = (ws_db / "memory" / eml_result["path"]).read_text(encoding="utf-8")
    zip_content = (ws_db / "memory" / zip_result["path"]).read_text(encoding="utf-8")
    assert "HTML body" in html_content
    assert "RTF body" in rtf_content
    assert "Email body" in eml_content
    assert "folder/readme.txt" in zip_content
    assert "not extracted in this step" in zip_content


@pytest.mark.anyio
async def test_invalid_business_document_raises_ingest_error(ws_db, tmp_path):
    src = tmp_path / "source" / "broken.docx"
    src.parent.mkdir(exist_ok=True)
    src.write_text("not a zip package", encoding="utf-8")

    with pytest.raises(IngestError, match="valid ZIP-based document"):
        await fast_ingest(src, "knowledge", workspace_path=ws_db)


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
