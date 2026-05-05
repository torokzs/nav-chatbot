# METADATA ----------
# title: 02 - Parse & Chunk NAV PDFs
# description: Uses Azure Document Intelligence prebuilt-layout to parse PDFs, then creates section-aware chunks with metadata

# COMMAND ----------

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tiktoken
import requests
from pyspark.sql import Row

try:
    from notebookutils import mssparkutils  # type: ignore
except ImportError:
    try:
        import mssparkutils  # type: ignore
    except ImportError as exc:
        raise RuntimeError("This notebook must run inside Microsoft Fabric or Synapse.") from exc


RAW_RELATIVE_PATH = os.getenv("RAW_RELATIVE_PATH", "raw/2026")
CHUNKS_TABLE = os.getenv("CHUNKS_TABLE", "nav_chunks")
DOCUMENTS_TABLE = os.getenv("DOCUMENTS_TABLE", "nav_documents")
DOC_INTEL_ENDPOINT = os.getenv("DOCUMENT_INTELLIGENCE_ENDPOINT", "https://di-whnqec-6otgod.cognitiveservices.azure.com")
TOKEN_TARGET = int(os.getenv("CHUNK_TOKEN_TARGET", "700"))
TOKEN_OVERLAP = int(os.getenv("CHUNK_TOKEN_OVERLAP", "100"))
TOKENIZER = tiktoken.get_encoding("cl100k_base")
REFERENCE_PATTERN = re.compile(r"(\d{1,3})\.\s*számú információs füzet", re.IGNORECASE)
DATE_PATTERNS = (
    re.compile(r"(20\d{2})[_-](\d{2})[_-](\d{2})"),
    re.compile(r"(20\d{2})(\d{2})(\d{2})"),
)
MOUNT_PATH = "/lakehouse/default/Files/" + RAW_RELATIVE_PATH


def get_cog_token() -> str:
    """Get cognitive services token via Fabric-native mssparkutils."""
    return mssparkutils.credentials.getToken("https://cognitiveservices.azure.com")


# COMMAND ----------

@dataclass
class OrderedElement:
    kind: str
    text: str
    page_from: int
    page_to: int
    order: int
    role: str | None = None



def files_path(relative_path: str = "") -> str:
    relative_path = relative_path.strip("/")
    return f"Files/{relative_path}" if relative_path else "Files"



def local_files_path(relative_path: str = "") -> str:
    relative_path = relative_path.strip("/")
    suffix = f"/{relative_path}" if relative_path else ""
    return f"/lakehouse/default/Files{suffix}"



def list_pdf_entries(path: str) -> list[object]:
    return sorted(
        [entry for entry in notebook_fs.ls(path) if entry.name.lower().endswith(".pdf")],
        key=lambda item: item.name,
    )



def read_pdf_bytes(file_name: str) -> bytes:
    pdf_path = Path(local_files_path(f"{RAW_RELATIVE_PATH}/{file_name}"))
    with pdf_path.open("rb") as handle:
        return handle.read()



def token_count(text: str) -> int:
    return len(TOKENIZER.encode(text))



def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())



def extract_fuzet_szam(file_name: str) -> str:
    match = re.match(r"^(\d{1,3})", file_name)
    return match.group(1) if match else "unknown"



def extract_kozzeteve(file_name: str) -> str | None:
    for pattern in DATE_PATTERNS:
        match = pattern.search(file_name)
        if match:
            return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    return None



def fallback_title_from_filename(file_name: str) -> str:
    base_name = Path(file_name).stem
    without_prefix = re.sub(r"^\d{1,3}_", "", base_name)
    without_date = re.sub(r"_20\d{2}(?:[_-]?\d{2}){0,2}$", "", without_prefix)
    cleaned = without_date.replace("_", " ")
    return normalize_text(cleaned)


# COMMAND ----------


def analyze_pdf_bytes_rest(pdf_bytes: bytes, file_name: str) -> dict:
    """Parse PDF via Document Intelligence REST API using Fabric-native token."""
    url = f"{DOC_INTEL_ENDPOINT}/documentintelligence/documentModels/prebuilt-layout:analyze?outputContentFormat=markdown&api-version=2024-11-30"
    for attempt in range(5):
        token = get_cog_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/pdf"}
        resp = requests.post(url, headers=headers, data=pdf_bytes, timeout=120)
        if resp.status_code == 202:
            op_url = resp.headers["Operation-Location"]
            # Poll for completion
            for _ in range(120):
                time.sleep(3)
                poll = requests.get(op_url, headers={"Authorization": f"Bearer {get_cog_token()}"})
                data = poll.json()
                status = data.get("status")
                if status == "succeeded":
                    return data["analyzeResult"]
                elif status == "failed":
                    raise RuntimeError(f"Doc Intelligence analysis failed for {file_name}: {data}")
            raise RuntimeError(f"Timeout waiting for Doc Intelligence to parse {file_name}")
        elif resp.status_code == 429:
            delay = int(resp.headers.get("Retry-After", 5 * (2 ** attempt)))
            print(f"Rate limited while parsing {file_name}. Retrying in {delay}s.")
            time.sleep(delay)
        else:
            raise RuntimeError(f"Doc Intelligence error {resp.status_code} for {file_name}: {resp.text[:200]}")



def paragraph_page(paragraph: Any) -> int:
    regions = getattr(paragraph, "bounding_regions", None) or []
    if regions:
        return int(regions[0].page_number)
    return 1



def paragraph_order(paragraph: Any, fallback: int) -> int:
    spans = getattr(paragraph, "spans", None) or []
    if spans:
        return int(spans[0].offset)
    return fallback



def table_pages(table: Any) -> tuple[int, int]:
    regions = getattr(table, "bounding_regions", None) or []
    if not regions:
        return 1, 1
    pages = sorted(int(region.page_number) for region in regions)
    return pages[0], pages[-1]



def table_order(table: Any, fallback: int) -> int:
    spans = getattr(table, "spans", None) or []
    if spans:
        return int(spans[0].offset)
    return fallback


# COMMAND ----------


def table_to_markdown(table: Any) -> str:
    row_count = int(getattr(table, "row_count", 0) or 0)
    col_count = int(getattr(table, "column_count", 0) or 0)
    if row_count <= 0 or col_count <= 0:
        return ""

    grid = [["" for _ in range(col_count)] for _ in range(row_count)]
    for cell in getattr(table, "cells", None) or []:
        value = normalize_text(getattr(cell, "content", ""))
        row_index = int(getattr(cell, "row_index", 0))
        col_index = int(getattr(cell, "column_index", 0))
        row_span = int(getattr(cell, "row_span", 1) or 1)
        col_span = int(getattr(cell, "column_span", 1) or 1)
        for row_offset in range(row_span):
            for col_offset in range(col_span):
                target_row = min(row_index + row_offset, row_count - 1)
                target_col = min(col_index + col_offset, col_count - 1)
                if not grid[target_row][target_col]:
                    grid[target_row][target_col] = value

    header = grid[0]
    divider = ["---"] * col_count
    body = grid[1:] if row_count > 1 else []
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(divider) + " |",
    ]
    for row in body:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def table_to_markdown_dict(table: dict) -> str:
    """Convert table dict (from REST API response) to markdown."""
    row_count = int(table.get("rowCount", 0))
    col_count = int(table.get("columnCount", 0))
    if row_count <= 0 or col_count <= 0:
        return ""

    grid = [["" for _ in range(col_count)] for _ in range(row_count)]
    for cell in table.get("cells", []):
        value = normalize_text(cell.get("content", ""))
        row_index = int(cell.get("rowIndex", 0))
        col_index = int(cell.get("columnIndex", 0))
        row_span = int(cell.get("rowSpan", 1))
        col_span = int(cell.get("columnSpan", 1))
        for row_offset in range(row_span):
            for col_offset in range(col_span):
                target_row = min(row_index + row_offset, row_count - 1)
                target_col = min(col_index + col_offset, col_count - 1)
                if not grid[target_row][target_col]:
                    grid[target_row][target_col] = value

    header = grid[0]
    divider = ["---"] * col_count
    body = grid[1:] if row_count > 1 else []
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(divider) + " |",
    ]
    for row in body:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)



def infer_heading_level(text: str, role: str | None) -> int:
    if role == "title":
        return 1
    if re.match(r"^[IVXLCDM]+\.\s", text, re.IGNORECASE):
        return 2
    if re.match(r"^\d+\.\d+\.\d+", text):
        return 5
    if re.match(r"^\d+\.\d+", text):
        return 4
    if re.match(r"^\d+\.", text):
        return 3
    if text.isupper() and len(text.split()) <= 12:
        return 3
    return 3



def sentence_split(text: str) -> list[str]:
    text = normalize_text(text)
    if not text:
        return []
    parts = re.split(r"(?<=[.!?])\s+(?=[A-ZÁÉÍÓÖŐÚÜŰ0-9])", text)
    return [part.strip() for part in parts if part.strip()]


# COMMAND ----------


def extract_ordered_elements(result: Any) -> list[OrderedElement]:
    ordered: list[OrderedElement] = []

    # Support both dict (REST API response) and SDK object
    paragraphs = result.get("paragraphs", []) if isinstance(result, dict) else (getattr(result, "paragraphs", None) or [])
    tables = result.get("tables", []) if isinstance(result, dict) else (getattr(result, "tables", None) or [])

    for index, paragraph in enumerate(paragraphs):
        if isinstance(paragraph, dict):
            text = normalize_text(paragraph.get("content", ""))
            role = paragraph.get("role")
            regions = paragraph.get("boundingRegions", [])
            page = int(regions[0]["pageNumber"]) if regions else 1
            spans = paragraph.get("spans", [])
            order = int(spans[0]["offset"]) if spans else index
        else:
            text = normalize_text(getattr(paragraph, "content", ""))
            role = getattr(paragraph, "role", None)
            page = paragraph_page(paragraph)
            order = paragraph_order(paragraph, index)
        if not text:
            continue
        ordered.append(
            OrderedElement(
                kind="paragraph",
                text=text,
                page_from=page,
                page_to=page,
                order=order,
                role=role,
            )
        )

    fallback_order = max([item.order for item in ordered], default=0) + 1
    for index, table in enumerate(tables):
        if isinstance(table, dict):
            markdown = table_to_markdown_dict(table)
            regions = table.get("boundingRegions", [])
            if regions:
                page_from = int(regions[0]["pageNumber"])
                page_to = int(regions[-1]["pageNumber"])
            else:
                page_from = page_to = 1
            spans = table.get("spans", [])
            order_val = int(spans[0]["offset"]) if spans else fallback_order + index
        else:
            markdown = table_to_markdown(table)
            page_from, page_to = table_pages(table)
            order_val = table_order(table, fallback_order + index)
        if not markdown:
            continue
        ordered.append(
            OrderedElement(
                kind="table",
                text=markdown,
                page_from=page_from,
                page_to=page_to,
                order=order_val,
                role="table",
            )
        )

    return sorted(ordered, key=lambda item: (item.page_from, item.order, 0 if item.kind == "paragraph" else 1))



def chunk_sentence_entries(
    entries: list[dict[str, Any]],
    file_name: str,
    fuzet_szam: str,
    fuzet_cim: str,
    kozzeteve: str | None,
    breadcrumb: str,
    start_index: int,
) -> tuple[list[dict[str, Any]], int]:
    chunks: list[dict[str, Any]] = []
    buffer: list[dict[str, Any]] = []
    sequence = start_index

    def make_chunk(items: list[dict[str, Any]]) -> dict[str, Any]:
        body = " ".join(item["text"] for item in items).strip()
        chunk_text = f"{breadcrumb}\n\n{body}" if breadcrumb else body
        pages = [item["page"] for item in items]
        references = sorted(set(REFERENCE_PATTERN.findall(chunk_text)))
        digest_source = f"{file_name}|{sequence}|{min(pages)}|{max(pages)}|{chunk_text}".encode("utf-8")
        return {
            "chunk_id": hashlib.sha1(digest_source).hexdigest()[:24],
            "fuzet_szam": fuzet_szam,
            "fuzet_cim": fuzet_cim,
            "breadcrumb": breadcrumb,
            "page_from": min(pages),
            "page_to": max(pages),
            "kozzeteve": kozzeteve,
            "hivatkozott_fuzetek": json.dumps(references, ensure_ascii=False),
            "content_type": "text",
            "content": chunk_text,
            "token_count": token_count(chunk_text),
        }

    for entry in entries:
        candidate = buffer + [entry]
        candidate_text = f"{breadcrumb}\n\n" + " ".join(item["text"] for item in candidate)
        if buffer and token_count(candidate_text) > TOKEN_TARGET:
            chunks.append(make_chunk(buffer))
            sequence += 1
            overlap: list[dict[str, Any]] = []
            overlap_tokens = 0
            for previous in reversed(buffer):
                sentence_tokens = token_count(previous["text"])
                if overlap and overlap_tokens + sentence_tokens > TOKEN_OVERLAP:
                    break
                overlap.insert(0, previous)
                overlap_tokens += sentence_tokens
            buffer = overlap + [entry]
        else:
            buffer = candidate

    if buffer:
        chunks.append(make_chunk(buffer))
        sequence += 1

    return chunks, sequence


# COMMAND ----------


def build_chunks_for_document(file_name: str, result: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    ordered_elements = extract_ordered_elements(result)
    fuzet_szam = extract_fuzet_szam(file_name)
    kozzeteve = extract_kozzeteve(file_name)
    headings = [item.text for item in ordered_elements if item.role in {"title", "sectionHeading"}]
    fuzet_cim = headings[0] if headings else fallback_title_from_filename(file_name)

    section_stack: list[tuple[int, str]] = [(1, fuzet_cim)]
    sentence_entries: list[dict[str, Any]] = []
    chunks: list[dict[str, Any]] = []
    sequence = 1

    def current_breadcrumb() -> str:
        return " / ".join(title for _, title in section_stack if title)

    def flush_sentences() -> None:
        nonlocal sentence_entries, chunks, sequence
        if not sentence_entries:
            return
        text_chunks, sequence = chunk_sentence_entries(
            sentence_entries,
            file_name=file_name,
            fuzet_szam=fuzet_szam,
            fuzet_cim=fuzet_cim,
            kozzeteve=kozzeteve,
            breadcrumb=current_breadcrumb(),
            start_index=sequence,
        )
        chunks.extend(text_chunks)
        sentence_entries = []

    for element in ordered_elements:
        if element.role in {"title", "sectionHeading"}:
            flush_sentences()
            level = infer_heading_level(element.text, element.role)
            while section_stack and section_stack[-1][0] >= level:
                section_stack.pop()
            section_stack.append((level, element.text))
            continue

        if element.kind == "table":
            flush_sentences()
            table_breadcrumb = current_breadcrumb()
            table_text = f"{table_breadcrumb}\n\n{element.text}" if table_breadcrumb else element.text
            references = sorted(set(REFERENCE_PATTERN.findall(table_text)))
            digest_source = f"{file_name}|table|{sequence}|{element.page_from}|{element.page_to}|{table_text}".encode("utf-8")
            chunks.append(
                {
                    "chunk_id": hashlib.sha1(digest_source).hexdigest()[:24],
                    "fuzet_szam": fuzet_szam,
                    "fuzet_cim": fuzet_cim,
                    "breadcrumb": table_breadcrumb,
                    "page_from": element.page_from,
                    "page_to": element.page_to,
                    "kozzeteve": kozzeteve,
                    "hivatkozott_fuzetek": json.dumps(references, ensure_ascii=False),
                    "content_type": "table",
                    "content": table_text,
                    "token_count": token_count(table_text),
                }
            )
            sequence += 1
            continue

        paragraph_text = element.text
        if element.role == "footnote":
            paragraph_text = f"Footnote: {paragraph_text}"
        for sentence in sentence_split(paragraph_text):
            sentence_entries.append({"text": sentence, "page": element.page_from})

    flush_sentences()

    document_row = {
        "fuzet_szam": fuzet_szam,
        "fuzet_cim": fuzet_cim,
        "kozzeteve": kozzeteve,
        "total_pages": len(result.get("pages", []) if isinstance(result, dict) else getattr(result, "pages", None) or []),
        "chunk_count": len(chunks),
        "content_summary": normalize_text(" ".join(chunk["content"] for chunk in chunks)[:500]),
    }
    return chunks, document_row


# COMMAND ----------

client = None  # No SDK client needed — using REST API via mssparkutils
pdf_files = sorted([f for f in os.listdir(MOUNT_PATH) if f.lower().endswith(".pdf")])
all_chunks: list[dict[str, Any]] = []
document_rows: list[dict[str, Any]] = []
failed_files: list[dict[str, str]] = []

for index, file_name in enumerate(pdf_files, start=1):
    print(f"[{index}/{len(pdf_files)}] Parsing {file_name}")
    try:
        pdf_path = os.path.join(MOUNT_PATH, file_name)
        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()
        result = analyze_pdf_bytes_rest(pdf_bytes, file_name)
        chunks, document_row = build_chunks_for_document(file_name, result)
        all_chunks.extend(chunks)
        document_rows.append(document_row)
        print(f"Created {len(chunks)} chunks for {file_name}")
    except Exception as exc:
        failed_files.append({"file_name": file_name, "error": str(exc)})
        print(f"Failed to parse {file_name}: {exc}")

if not all_chunks:
    raise RuntimeError("No chunks were created. Check the input files and Document Intelligence configuration.")

chunks_df = spark.createDataFrame([Row(**row) for row in all_chunks])
documents_df = spark.createDataFrame([Row(**row) for row in document_rows])

(
    chunks_df.write.format("delta")
    .mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(CHUNKS_TABLE)
)
(
    documents_df.write.format("delta")
    .mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(DOCUMENTS_TABLE)
)

print(f"Wrote {len(all_chunks)} rows to {CHUNKS_TABLE}")
print(f"Wrote {len(document_rows)} rows to {DOCUMENTS_TABLE}")
print(f"Failed files: {len(failed_files)}")
for item in failed_files:
    print(f" - {item['file_name']}: {item['error']}")
