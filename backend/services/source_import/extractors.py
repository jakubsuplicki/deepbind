from __future__ import annotations

import html
import re
import zipfile
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from email.message import Message
from pathlib import Path, PurePosixPath
from typing import Iterable, Optional

from defusedxml import ElementTree as ET


BUSINESS_EXTRACTOR_EXTENSIONS = {
    ".docx",
    ".xlsx",
    ".pptx",
    ".html",
    ".htm",
    ".rtf",
    ".eml",
    ".zip",
}

MAX_ZIP_ENTRIES = 2_000
MAX_ZIP_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
MAX_RENDERED_CHARS = 300_000
XLSX_MAX_PREVIEW_ROWS = 80
XLSX_MAX_PREVIEW_COLS = 30
ZIP_PREVIEW_LIMIT = 500


class ExtractorError(Exception):
    pass


@dataclass
class ExtractedDocument:
    title: str
    markdown: str
    source_type: str
    warnings: list[str] = field(default_factory=list)


def supported_business_extensions() -> set[str]:
    return set(BUSINESS_EXTRACTOR_EXTENSIONS)


def extract_business_document(file_path: Path, display_name: Optional[str] = None) -> ExtractedDocument:
    name = display_name or file_path.name
    ext = Path(name).suffix.lower()
    if ext == ".docx":
        return _extract_docx(file_path, name)
    if ext == ".xlsx":
        return _extract_xlsx(file_path, name)
    if ext == ".pptx":
        return _extract_pptx(file_path, name)
    if ext in {".html", ".htm"}:
        return _extract_html(file_path, name)
    if ext == ".rtf":
        return _extract_rtf(file_path, name)
    if ext == ".eml":
        return _extract_eml(file_path, name)
    if ext == ".zip":
        return _extract_zip_inventory(file_path, name)
    raise ExtractorError(f"Unsupported business document type: {ext}")


def _cap(text: str) -> str:
    if len(text) <= MAX_RENDERED_CHARS:
        return text
    return text[:MAX_RENDERED_CHARS].rstrip() + "\n\n[content truncated]\n"


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _read_zip_xml(zf: zipfile.ZipFile, name: str) -> ET.Element:
    try:
        with zf.open(name) as fh:
            return ET.parse(fh).getroot()
    except KeyError as exc:
        raise ExtractorError(f"Missing required Office XML part: {name}") from exc
    except ET.ParseError as exc:
        raise ExtractorError(f"Could not parse Office XML part: {name}") from exc


def _validate_zip(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    infos = zf.infolist()
    if len(infos) > MAX_ZIP_ENTRIES:
        raise ExtractorError(f"Archive has too many entries ({len(infos)} > {MAX_ZIP_ENTRIES})")
    total = 0
    for info in infos:
        path = PurePosixPath(info.filename)
        if info.filename.startswith("/") or ".." in path.parts:
            raise ExtractorError("Archive contains an unsafe path")
        total += max(info.file_size, 0)
        if total > MAX_ZIP_UNCOMPRESSED_BYTES:
            raise ExtractorError("Archive is too large after decompression")
    return infos


def _open_safe_zip(path: Path) -> zipfile.ZipFile:
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise ExtractorError("File is not a valid ZIP-based document") from exc
    _validate_zip(zf)
    return zf


def _xml_text(node: ET.Element) -> str:
    parts: list[str] = []
    for child in node.iter():
        name = _local_name(child.tag)
        if name in {"t", "instrText"} and child.text:
            parts.append(child.text)
        elif name == "tab":
            parts.append("\t")
        elif name in {"br", "cr"}:
            parts.append("\n")
    return "".join(parts).strip()


def _docx_paragraph_text(node: ET.Element) -> str:
    return re.sub(r"[ \t]+\n", "\n", _xml_text(node)).strip()


def _docx_paragraph_heading(node: ET.Element) -> int:
    for child in node.iter():
        if _local_name(child.tag) != "pStyle":
            continue
        value = child.attrib.get("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val", "")
        match = re.search(r"heading([1-6])", value, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return 0


def _markdown_table(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    normalized = [row + [""] * (width - len(row)) for row in rows]
    header = normalized[0]
    body = normalized[1:]

    def clean(cell: str) -> str:
        return cell.replace("\n", " ").replace("|", "\\|").strip()

    lines = [
        "| " + " | ".join(clean(cell) for cell in header) + " |",
        "| " + " | ".join("---" for _ in range(width)) + " |",
    ]
    for row in body:
        lines.append("| " + " | ".join(clean(cell) for cell in row) + " |")
    return "\n".join(lines)


def _docx_table_markdown(node: ET.Element) -> str:
    rows: list[list[str]] = []
    for row in [child for child in node if _local_name(child.tag) == "tr"]:
        cells: list[str] = []
        for cell in [child for child in row if _local_name(child.tag) == "tc"]:
            paragraphs = [
                _docx_paragraph_text(p)
                for p in cell.iter()
                if _local_name(p.tag) == "p"
            ]
            cells.append(" ".join(text for text in paragraphs if text))
        if any(cells):
            rows.append(cells)
    return _markdown_table(rows)


def _extract_docx(path: Path, display_name: str) -> ExtractedDocument:
    title = Path(display_name).stem
    warnings: list[str] = []
    with _open_safe_zip(path) as zf:
        root = _read_zip_xml(zf, "word/document.xml")
        body = next((child for child in root.iter() if _local_name(child.tag) == "body"), None)
        if body is None:
            raise ExtractorError("DOCX document body not found")

        blocks: list[str] = [f"# {title}", ""]
        for child in body:
            name = _local_name(child.tag)
            if name == "p":
                text = _docx_paragraph_text(child)
                if not text:
                    continue
                heading = _docx_paragraph_heading(child)
                if heading:
                    level = min(heading + 1, 6)
                    blocks.append(f"{'#' * level} {text}")
                else:
                    blocks.append(text)
                blocks.append("")
            elif name == "tbl":
                table = _docx_table_markdown(child)
                if table:
                    blocks.extend([table, ""])
        markdown = _cap("\n".join(blocks).strip() + "\n")
    return ExtractedDocument(title=title, markdown=markdown, source_type="docx", warnings=warnings)


def _xlsx_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    try:
        root = _read_zip_xml(zf, "xl/sharedStrings.xml")
    except ExtractorError:
        return []
    strings: list[str] = []
    for si in [child for child in root if _local_name(child.tag) == "si"]:
        text = "".join(t.text or "" for t in si.iter() if _local_name(t.tag) == "t").strip()
        strings.append(text)
    return strings


def _xlsx_sheet_paths(zf: zipfile.ZipFile) -> list[tuple[str, str]]:
    workbook = _read_zip_xml(zf, "xl/workbook.xml")
    rels = _read_zip_xml(zf, "xl/_rels/workbook.xml.rels")
    rel_map: dict[str, str] = {}
    for rel in rels:
        if _local_name(rel.tag) != "Relationship":
            continue
        rel_id = rel.attrib.get("Id", "")
        target = rel.attrib.get("Target", "")
        if rel_id and target:
            rel_map[rel_id] = "xl/" + target.lstrip("/")

    sheets: list[tuple[str, str]] = []
    for sheet in workbook.iter():
        if _local_name(sheet.tag) != "sheet":
            continue
        name = sheet.attrib.get("name", "Sheet")
        rel_id = sheet.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id", "")
        path = rel_map.get(rel_id)
        if path:
            sheets.append((name, path))
    return sheets


def _xlsx_cell_value(cell: ET.Element, shared_strings: list[str]) -> str:
    cell_type = cell.attrib.get("t", "")
    formula = next((child.text or "" for child in cell if _local_name(child.tag) == "f"), "")
    value = next((child.text or "" for child in cell if _local_name(child.tag) == "v"), "")
    if cell_type == "s" and value:
        try:
            value = shared_strings[int(value)]
        except (ValueError, IndexError):
            value = ""
    elif cell_type == "inlineStr":
        value = "".join(t.text or "" for t in cell.iter() if _local_name(t.tag) == "t")
    if formula:
        return f"={formula}" + (f" (cached: {value})" if value else "")
    return value


def _extract_xlsx(path: Path, display_name: str) -> ExtractedDocument:
    title = Path(display_name).stem
    warnings: list[str] = []
    with _open_safe_zip(path) as zf:
        shared_strings = _xlsx_shared_strings(zf)
        sheets = _xlsx_sheet_paths(zf)
        blocks: list[str] = [f"# {title}", ""]
        for sheet_name, sheet_path in sheets:
            root = _read_zip_xml(zf, sheet_path)
            rows: list[list[str]] = []
            for row in root.iter():
                if _local_name(row.tag) != "row":
                    continue
                values = [
                    _xlsx_cell_value(cell, shared_strings)
                    for cell in row
                    if _local_name(cell.tag) == "c"
                ]
                if any(values):
                    rows.append(values[:XLSX_MAX_PREVIEW_COLS])
                if len(rows) >= XLSX_MAX_PREVIEW_ROWS:
                    warnings.append(f"{sheet_name}: preview limited to {XLSX_MAX_PREVIEW_ROWS} rows")
                    break
            blocks.extend([f"## {sheet_name}", ""])
            if rows:
                blocks.extend([_markdown_table(rows), ""])
            else:
                blocks.extend(["No populated cells found.", ""])
    markdown = _cap("\n".join(blocks).strip() + "\n")
    return ExtractedDocument(title=title, markdown=markdown, source_type="xlsx", warnings=warnings)


def _slide_sort_key(name: str) -> int:
    match = re.search(r"slide(\d+)\.xml$", name)
    return int(match.group(1)) if match else 0


def _extract_pptx(path: Path, display_name: str) -> ExtractedDocument:
    title = Path(display_name).stem
    warnings: list[str] = []
    with _open_safe_zip(path) as zf:
        slide_names = sorted(
            [name for name in zf.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", name)],
            key=_slide_sort_key,
        )
        blocks: list[str] = [f"# {title}", ""]
        for idx, slide_name in enumerate(slide_names, start=1):
            root = _read_zip_xml(zf, slide_name)
            texts = [t.text.strip() for t in root.iter() if _local_name(t.tag) == "t" and t.text and t.text.strip()]
            blocks.extend([f"## Slide {idx}", ""])
            if texts:
                blocks.extend([f"- {text}" for text in texts])
                blocks.append("")
            else:
                blocks.extend(["No readable text found.", ""])
    markdown = _cap("\n".join(blocks).strip() + "\n")
    return ExtractedDocument(title=title, markdown=markdown, source_type="pptx", warnings=warnings)


def _extract_html(path: Path, display_name: str) -> ExtractedDocument:
    import trafilatura
    from bs4 import BeautifulSoup
    from markdownify import markdownify as md

    raw = path.read_text(encoding="utf-8", errors="replace")
    extracted = trafilatura.extract(
        raw,
        include_comments=False,
        include_tables=True,
        output_format="html",
    )
    html_body = extracted or raw
    soup = BeautifulSoup(raw, "html.parser")
    title = (soup.title.string.strip() if soup.title and soup.title.string else Path(display_name).stem)
    markdown = _cap(f"# {title}\n\n{md(html_body, heading_style='ATX').strip()}\n")
    return ExtractedDocument(title=title, markdown=markdown, source_type="html")


def _decode_text_file(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _strip_rtf(text: str) -> str:
    text = re.sub(
        r"\\'([0-9a-fA-F]{2})",
        lambda match: bytes.fromhex(match.group(1)).decode("cp1252", errors="replace"),
        text,
    )
    text = re.sub(r"\\(?:par|line)\b[ ]?", "\n", text)
    text = re.sub(r"\\tab\b[ ]?", "\t", text)
    text = re.sub(r"\\[{}\\]", lambda match: match.group(0)[1], text)
    text = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", text)
    text = re.sub(r"\\.", "", text)
    text = text.replace("{", "").replace("}", "")
    text = html.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_rtf(path: Path, display_name: str) -> ExtractedDocument:
    title = Path(display_name).stem
    body = _strip_rtf(_decode_text_file(path))
    markdown = _cap(f"# {title}\n\n{body}\n")
    return ExtractedDocument(title=title, markdown=markdown, source_type="rtf")


def _part_filename(part: Message) -> str:
    filename = part.get_filename()
    return filename or "(unnamed attachment)"


def _extract_eml(path: Path, display_name: str) -> ExtractedDocument:
    from markdownify import markdownify as md

    with path.open("rb") as fh:
        msg = BytesParser(policy=policy.default).parse(fh)

    subject = str(msg.get("subject") or Path(display_name).stem)
    from_ = str(msg.get("from") or "")
    to = str(msg.get("to") or "")
    date = str(msg.get("date") or "")
    attachments: list[str] = []
    body_parts: list[str] = []
    html_parts: list[str] = []

    if msg.is_multipart():
        parts: Iterable[Message] = msg.walk()
    else:
        parts = [msg]

    for part in parts:
        if part.is_multipart():
            continue
        disposition = (part.get_content_disposition() or "").lower()
        content_type = part.get_content_type()
        if disposition == "attachment":
            attachments.append(_part_filename(part))
            continue
        try:
            content = part.get_content()
        except Exception:
            continue
        if content_type == "text/plain" and isinstance(content, str):
            body_parts.append(content.strip())
        elif content_type == "text/html" and isinstance(content, str):
            html_parts.append(md(content, heading_style="ATX").strip())

    body = "\n\n".join(part for part in body_parts if part)
    if not body:
        body = "\n\n".join(part for part in html_parts if part)
    if not body:
        body = "(No readable message body found.)"

    lines = [
        f"# {subject}",
        "",
        f"- From: {from_}" if from_ else "",
        f"- To: {to}" if to else "",
        f"- Date: {date}" if date else "",
        "",
        body,
    ]
    if attachments:
        lines.extend(["", "## Attachments", ""])
        lines.extend(f"- {name}" for name in attachments)
    markdown = _cap("\n".join(line for line in lines if line != "").strip() + "\n")
    return ExtractedDocument(title=subject, markdown=markdown, source_type="eml")


def _extract_zip_inventory(path: Path, display_name: str) -> ExtractedDocument:
    title = Path(display_name).stem
    warnings: list[str] = []
    with _open_safe_zip(path) as zf:
        infos = [info for info in zf.infolist() if not info.is_dir()]
        if len(infos) > ZIP_PREVIEW_LIMIT:
            warnings.append(f"Archive listing limited to first {ZIP_PREVIEW_LIMIT} files")
        rows = [["Path", "Size"]]
        for info in infos[:ZIP_PREVIEW_LIMIT]:
            rows.append([info.filename, str(info.file_size)])
    body = _markdown_table(rows) if len(rows) > 1 else "No files found in archive."
    markdown = _cap(
        f"# {title}\n\n"
        "This archive was imported as a safe inventory. Files inside the archive are not extracted in this step.\n\n"
        f"{body}\n"
    )
    return ExtractedDocument(title=title, markdown=markdown, source_type="zip", warnings=warnings)
