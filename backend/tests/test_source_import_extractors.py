import zipfile
from email.message import EmailMessage
from pathlib import Path

import pytest

from services.source_import import extractors
from services.source_import.extractors import ExtractorError, extract_business_document


def _write_zip(path: Path, files: dict[str, str | bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in files.items():
            if isinstance(content, bytes):
                zf.writestr(name, content)
            else:
                zf.writestr(name, content.encode("utf-8"))
    return path


def _mark_first_zip_member_encrypted(path: Path) -> None:
    data = bytearray(path.read_bytes())
    for signature, flag_offset in (
        (b"PK\x03\x04", 6),
        (b"PK\x01\x02", 8),
    ):
        index = data.find(signature)
        assert index >= 0
        offset = index + flag_offset
        flags = int.from_bytes(data[offset:offset + 2], "little") | 0x1
        data[offset:offset + 2] = flags.to_bytes(2, "little")
    path.write_bytes(data)


def _core_properties(title: str) -> str:
    return f"""
    <cp:coreProperties
        xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
        xmlns:dc="http://purl.org/dc/elements/1.1/">
      <dc:title>{title}</dc:title>
    </cp:coreProperties>
    """


def _xlsx_col_name(index: int) -> str:
    name = ""
    while index:
        index, rem = divmod(index - 1, 26)
        name = chr(65 + rem) + name
    return name


def _inline_cell(row: int, col: int, value: str) -> str:
    ref = f"{_xlsx_col_name(col)}{row}"
    return f'<c r="{ref}" t="inlineStr"><is><t>{value}</t></is></c>'


def test_docx_uses_core_title_and_preserves_tables(tmp_path):
    src = _write_zip(
        tmp_path / "source" / "filename.docx",
        {
            "docProps/core.xml": _core_properties("Northstar Pilot Proposal"),
            "word/document.xml": """
            <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
              <w:body>
                <w:p>
                  <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
                  <w:r><w:t>Executive Summary</w:t></w:r>
                </w:p>
                <w:p><w:r><w:t>Client proposal text.</w:t></w:r></w:p>
                <w:tbl>
                  <w:tr>
                    <w:tc><w:p><w:r><w:t>Item</w:t></w:r></w:p></w:tc>
                    <w:tc><w:p><w:r><w:t>Cost</w:t></w:r></w:p></w:tc>
                  </w:tr>
                  <w:tr>
                    <w:tc><w:p><w:r><w:t>Discovery</w:t></w:r></w:p></w:tc>
                    <w:tc><w:p><w:r><w:t>1200</w:t></w:r></w:p></w:tc>
                  </w:tr>
                </w:tbl>
              </w:body>
            </w:document>
            """,
        },
    )

    doc = extract_business_document(src)

    assert doc.title == "Northstar Pilot Proposal"
    assert doc.source_type == "docx"
    assert "# Northstar Pilot Proposal" in doc.markdown
    assert "## Executive Summary" in doc.markdown
    assert "| Item | Cost |" in doc.markdown
    assert doc.warnings == []


def test_xlsx_preview_limits_rows_and_columns_with_warnings(tmp_path):
    rows_xml = "\n".join(
        f'<row r="{row}">'
        + "".join(_inline_cell(row, col, f"R{row}C{col}") for col in range(1, 33))
        + "</row>"
        for row in range(1, 82)
    )
    src = _write_zip(
        tmp_path / "source" / "pipeline.xlsx",
        {
            "docProps/core.xml": _core_properties("Revenue Pipeline"),
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
            "xl/worksheets/sheet1.xml": f"""
            <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
              <sheetData>{rows_xml}</sheetData>
            </worksheet>
            """,
        },
    )

    doc = extract_business_document(src)

    assert doc.title == "Revenue Pipeline"
    assert "## Pipeline" in doc.markdown
    assert "R80C30" in doc.markdown
    assert "R81C1" not in doc.markdown
    assert "R1C31" not in doc.markdown
    assert doc.warnings == [
        "Pipeline: preview limited to first 30 columns",
        "Pipeline: preview limited to 80 rows",
    ]


def test_xlsx_malformed_shared_strings_warns_without_failing(tmp_path):
    src = _write_zip(
        tmp_path / "source" / "pipeline.xlsx",
        {
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
            "xl/sharedStrings.xml": "<sst>",
            "xl/worksheets/sheet1.xml": """
            <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
              <sheetData>
                <row><c t="inlineStr"><is><t>Client</t></is></c><c><v>4200</v></c></row>
              </sheetData>
            </worksheet>
            """,
        },
    )

    doc = extract_business_document(src)

    assert "| Client | 4200 |" in doc.markdown
    assert doc.warnings == [
        "Shared strings unavailable: Could not parse Office XML part: xl/sharedStrings.xml"
    ]


def test_docx_malformed_document_xml_reports_safe_error(tmp_path):
    src = _write_zip(
        tmp_path / "source" / "broken.docx",
        {
            "word/document.xml": "<w:document>",
        },
    )

    with pytest.raises(ExtractorError, match="Could not parse Office XML part: word/document.xml"):
        extract_business_document(src)


def test_pptx_uses_core_title_and_warns_on_blank_deck(tmp_path):
    src = _write_zip(
        tmp_path / "source" / "empty-deck.pptx",
        {
            "docProps/core.xml": _core_properties("Board Update"),
            "ppt/presentation.xml": """
            <p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>
            """,
        },
    )

    doc = extract_business_document(src)

    assert doc.title == "Board Update"
    assert "No readable slides found." in doc.markdown
    assert doc.warnings == ["No slides found in presentation"]


def test_html_fallback_sanitizes_scripts_and_records_warning(tmp_path, monkeypatch):
    src = tmp_path / "source" / "page.html"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(
        """
        <html>
          <head><title>Client Page</title><script>secret()</script></head>
          <body><h1>Plan</h1><p>HTML body.</p><script>doNotImport()</script></body>
        </html>
        """,
        encoding="utf-8",
    )
    import trafilatura

    monkeypatch.setattr(trafilatura, "extract", lambda *_args, **_kwargs: None)

    doc = extract_business_document(src)

    assert doc.title == "Client Page"
    assert "HTML body" in doc.markdown
    assert "doNotImport" not in doc.markdown
    assert doc.warnings == ["HTML main content extraction unavailable; converted page body"]


def test_html_fallback_hides_parser_exception_details(tmp_path, monkeypatch):
    src = tmp_path / "source" / "page.html"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("<html><head><title>Saved Page</title></head><body><p>Body copy.</p></body></html>", encoding="utf-8")
    import trafilatura

    class ParserConfigError(Exception):
        pass

    def raise_parser_error(*_args, **_kwargs):
        raise ParserConfigError("internal parser detail")

    monkeypatch.setattr(trafilatura, "extract", raise_parser_error)

    doc = extract_business_document(src)

    assert "Body copy" in doc.markdown
    assert doc.warnings == ["HTML main content extraction unavailable; converted page body"]


def test_rtf_decodes_text_control_words(tmp_path):
    src = tmp_path / "source" / "memo.rtf"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(r"{\rtf1\ansi Caf\'e9 memo\par Next line.}", encoding="utf-8")

    doc = extract_business_document(src)

    assert r"\'e9" not in doc.markdown
    assert "Caf" in doc.markdown
    assert "Next line." in doc.markdown
    assert doc.warnings == []


def test_eml_extracts_cc_html_body_and_attachment_metadata(tmp_path):
    msg = EmailMessage()
    msg["Subject"] = "Follow up"
    msg["From"] = "a@example.com"
    msg["To"] = "b@example.com"
    msg["Cc"] = "c@example.com"
    msg.set_content("<p>Email <strong>body</strong>.</p>", subtype="html")
    msg.add_attachment(
        b"placeholder",
        maintype="application",
        subtype="pdf",
        filename="budget.pdf",
    )
    src = tmp_path / "source" / "message.eml"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(msg.as_bytes())

    doc = extract_business_document(src)

    assert doc.title == "Follow up"
    assert "- Cc: c@example.com" in doc.markdown
    assert "Email **body**." in doc.markdown
    assert "budget.pdf" in doc.markdown
    assert doc.warnings == ["1 attachment listed but not imported"]


def test_zip_inventory_preview_limit_is_visible(tmp_path, monkeypatch):
    monkeypatch.setattr(extractors, "ZIP_PREVIEW_LIMIT", 2)
    src = _write_zip(
        tmp_path / "source" / "archive.zip",
        {
            "a.txt": "A",
            "b.txt": "B",
            "c.txt": "C",
        },
    )

    doc = extract_business_document(src)

    assert "a.txt" in doc.markdown
    assert "b.txt" in doc.markdown
    assert "c.txt" not in doc.markdown
    assert doc.warnings == ["Archive listing limited to first 2 files"]


def test_zip_inventory_rejects_encrypted_members_with_safe_error(tmp_path):
    src = _write_zip(
        tmp_path / "source" / "locked.zip",
        {
            "secret.txt": "classified",
        },
    )
    _mark_first_zip_member_encrypted(src)

    with pytest.raises(ExtractorError, match="password-protected or encrypted"):
        extract_business_document(src)
