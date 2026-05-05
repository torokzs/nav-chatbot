from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, TypeVar

import tiktoken
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.ai.documentintelligence.models import AnalyzeDocumentRequest, DocumentContentFormat
from azure.ai.inference import ChatCompletionsClient, EmbeddingsClient
from azure.ai.inference.models import SystemMessage, UserMessage
from azure.core.exceptions import HttpResponseError, ResourceExistsError, ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    HnswAlgorithmConfiguration,
    HnswParameters,
    SearchField,
    SearchFieldDataType,
    SearchIndex,
    SearchableField,
    SemanticConfiguration,
    SemanticField,
    SemanticPrioritizedFields,
    SemanticSearch,
    SimpleField,
    SynonymMap,
    VectorSearch,
    VectorSearchProfile,
)
from dotenv import load_dotenv
from tqdm import tqdm

REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(REPO_ROOT / ".env")

TOKENIZER = tiktoken.get_encoding("cl100k_base")
TOKEN_TARGET = 700
TOKEN_OVERLAP = 100
EMBED_BATCH_SIZE = 16
UPLOAD_BATCH_SIZE = 100
VECTOR_DIMENSIONS = 3072
SYNONYM_MAP_NAME = "nav-tax-synonyms"
CHUNKS_INDEX_NAME = "nav-chunks"
DOCUMENTS_INDEX_NAME = "nav-documents"
VECTOR_PROFILE_NAME = "nav-hnsw-profile"
VECTOR_ALGORITHM_NAME = "nav-hnsw"
SEMANTIC_CONFIG_NAME = "default"
SYNONYMS = """
szja, személyi jövedelemadó
áfa, általános forgalmi adó, ÁFA
kata, kisadózó vállalkozók tételes adója, KATA
ekho, egyszerűsített közteherviselési hozzájárulás, EKHO
tao, társasági adó, TAO
szocho, szociális hozzájárulási adó
tbj, társadalombiztosítási járulék
kiva, kisvállalati adó, KIVA
""".strip()
REFERENCE_PATTERN = re.compile(r"(\d{1,3})\.\s*számú információs füzet", re.IGNORECASE)
DATE_PATTERNS = (
    re.compile(r"(20\d{2})[_-](\d{2})[_-](\d{2})"),
    re.compile(r"(20\d{2})(\d{2})(\d{2})"),
)
IGNORED_PARAGRAPH_ROLES = {"pageHeader", "pageFooter", "pageNumber"}
RETRYABLE_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504}
T = TypeVar("T")

logger = logging.getLogger("local_ingest")


@dataclass(slots=True)
class Settings:
    search_endpoint: str
    foundry_endpoint: str
    doc_intelligence_endpoint: str
    chat_deployment: str = "model-router"
    embedding_deployment: str = "text-embedding-3-large"

    @classmethod
    def from_env(cls) -> "Settings":
        search_endpoint = (os.getenv("AZURE_SEARCH_ENDPOINT") or os.getenv("AI_SEARCH_ENDPOINT") or "").rstrip("/")
        foundry_endpoint = (
            os.getenv("AZURE_AI_FOUNDRY_ENDPOINT")
            or os.getenv("AI_FOUNDRY_ENDPOINT")
            or ""
        ).rstrip("/")
        doc_intelligence_endpoint = (
            os.getenv("AZURE_DOC_INTELLIGENCE_ENDPOINT")
            or os.getenv("DOCUMENT_INTELLIGENCE_ENDPOINT")
            or ""
        ).rstrip("/")
        chat_deployment = (
            os.getenv("AZURE_AI_FOUNDRY_CHAT_DEPLOYMENT")
            or os.getenv("AI_FOUNDRY_CHAT_MODEL")
            or "model-router"
        )
        embedding_deployment = (
            os.getenv("AZURE_AI_FOUNDRY_EMBEDDING_DEPLOYMENT")
            or os.getenv("AI_FOUNDRY_EMBED_MODEL")
            or "text-embedding-3-large"
        )
        missing = [
            name
            for name, value in {
                "AZURE_SEARCH_ENDPOINT": search_endpoint,
                "AZURE_AI_FOUNDRY_ENDPOINT": foundry_endpoint,
                "AZURE_DOC_INTELLIGENCE_ENDPOINT": doc_intelligence_endpoint,
            }.items()
            if not value
        ]
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")
        return cls(
            search_endpoint=search_endpoint,
            foundry_endpoint=foundry_endpoint,
            doc_intelligence_endpoint=doc_intelligence_endpoint,
            chat_deployment=chat_deployment,
            embedding_deployment=embedding_deployment,
        )


@dataclass(slots=True)
class OrderedElement:
    kind: str
    text: str
    page_from: int
    page_to: int
    order: int
    role: str | None = None


@dataclass(slots=True)
class AzureClients:
    credential: DefaultAzureCredential
    doc_client: DocumentIntelligenceClient
    chat_client: ChatCompletionsClient
    embedding_client: EmbeddingsClient
    index_client: SearchIndexClient
    chunk_client: SearchClient
    document_client: SearchClient


class PDFProcessingError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local NAV PDF ingestion into Azure AI Search")
    parser.add_argument("--data-dir", default="../data", help="Directory containing PDF files (default: ../data)")
    parser.add_argument("--max-pdfs", type=int, default=None, help="Process at most N PDFs")
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip PDFs whose booklet id already has chunk documents in Azure AI Search",
    )
    parser.add_argument(
        "--no-summary",
        action="store_true",
        help="Skip per-chunk LLM summaries to reduce cost during testing",
    )
    return parser.parse_args()


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Suppress verbose Azure SDK logging
    logging.getLogger("azure").setLevel(logging.WARNING)
    logging.getLogger("azure.identity").setLevel(logging.WARNING)
    logging.getLogger("azure.core").setLevel(logging.WARNING)


def resolve_data_dir(raw_value: str) -> Path:
    path = Path(raw_value)
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    return path


def normalize_text(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def token_count(text: str) -> int:
    return len(TOKENIZER.encode(text))


def batched(items: Iterable[T], size: int) -> Iterator[list[T]]:
    iterator = iter(items)
    while True:
        batch = list(islice(iterator, size))
        if not batch:
            return
        yield batch


def retryable(operation_name: str, func: Callable[[], T], retries: int = 6, base_delay: float = 2.0) -> T:
    for attempt in range(1, retries + 1):
        try:
            return func()
        except HttpResponseError as exc:
            status_code = getattr(exc, "status_code", None)
            if status_code not in RETRYABLE_STATUS_CODES or attempt >= retries:
                raise
            retry_after = None
            response = getattr(exc, "response", None)
            if response is not None:
                retry_after = response.headers.get("Retry-After")
            if retry_after and str(retry_after).isdigit():
                delay = float(retry_after)
            else:
                delay = min(60.0, base_delay * (2 ** (attempt - 1)))
            logger.warning(
                "%s failed with status %s. Retrying in %.1f seconds (%s/%s).",
                operation_name,
                status_code,
                delay,
                attempt,
                retries,
            )
            time.sleep(delay)

    raise RuntimeError(f"{operation_name} failed after {retries} attempts")


def build_clients(settings: Settings) -> AzureClients:
    credential = DefaultAzureCredential()
    # Azure AI Services accounts need /openai/deployments/{name} appended for azure-ai-inference SDK
    chat_endpoint = f"{settings.foundry_endpoint.rstrip('/')}/openai/deployments/{settings.chat_deployment}"
    embedding_endpoint = f"{settings.foundry_endpoint.rstrip('/')}/openai/deployments/{settings.embedding_deployment}"
    return AzureClients(
        credential=credential,
        doc_client=DocumentIntelligenceClient(endpoint=settings.doc_intelligence_endpoint, credential=credential),
        chat_client=ChatCompletionsClient(
            endpoint=chat_endpoint,
            credential=credential,
            credential_scopes=["https://cognitiveservices.azure.com/.default"],
        ),
        embedding_client=EmbeddingsClient(
            endpoint=embedding_endpoint,
            credential=credential,
            credential_scopes=["https://cognitiveservices.azure.com/.default"],
        ),
        index_client=SearchIndexClient(endpoint=settings.search_endpoint, credential=credential),
        chunk_client=SearchClient(endpoint=settings.search_endpoint, index_name=CHUNKS_INDEX_NAME, credential=credential),
        document_client=SearchClient(
            endpoint=settings.search_endpoint,
            index_name=DOCUMENTS_INDEX_NAME,
            credential=credential,
        ),
    )


def close_clients(clients: AzureClients) -> None:
    for client in [
        clients.doc_client,
        clients.chat_client,
        clients.embedding_client,
        clients.index_client,
        clients.chunk_client,
        clients.document_client,
        clients.credential,
    ]:
        close = getattr(client, "close", None)
        if callable(close):
            try:
                close()
            except Exception as exc:  # pragma: no cover - defensive cleanup
                logger.debug("Ignoring cleanup error: %s", exc)


def ensure_synonym_map(index_client: SearchIndexClient) -> None:
    synonym_map = SynonymMap(name=SYNONYM_MAP_NAME, format="solr", synonyms=SYNONYMS)
    retryable("create synonym map", lambda: index_client.create_or_update_synonym_map(synonym_map))
    logger.info("Synonym map ready: %s", SYNONYM_MAP_NAME)


def build_vector_search() -> VectorSearch:
    return VectorSearch(
        algorithms=[
            HnswAlgorithmConfiguration(
                name=VECTOR_ALGORITHM_NAME,
                parameters=HnswParameters(metric="cosine"),
            )
        ],
        profiles=[
            VectorSearchProfile(
                name=VECTOR_PROFILE_NAME,
                algorithm_configuration_name=VECTOR_ALGORITHM_NAME,
            )
        ],
    )


def build_chunk_index() -> SearchIndex:
    fields = [
        SimpleField(name="chunk_id", type=SearchFieldDataType.String, key=True, filterable=True),
        SimpleField(name="fuzet_szam", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SearchableField(name="fuzet_cim", analyzer_name="hu.microsoft"),
        SearchableField(
            name="breadcrumb",
            analyzer_name="hu.microsoft",
            synonym_map_names=[SYNONYM_MAP_NAME],
        ),
        SimpleField(name="page_from", type=SearchFieldDataType.Int32, filterable=True, sortable=True),
        SimpleField(name="page_to", type=SearchFieldDataType.Int32, filterable=True, sortable=True),
        SimpleField(name="kozzeteve", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SearchField(
            name="hivatkozott_fuzetek",
            type=SearchFieldDataType.Collection(SearchFieldDataType.String),
            filterable=True,
            facetable=True,
        ),
        SimpleField(name="content_type", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SearchableField(
            name="content",
            analyzer_name="hu.microsoft",
            synonym_map_names=[SYNONYM_MAP_NAME],
        ),
        SearchableField(
            name="summary",
            analyzer_name="hu.microsoft",
            synonym_map_names=[SYNONYM_MAP_NAME],
        ),
        SearchField(
            name="content_vector",
            type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
            searchable=True,
            vector_search_dimensions=VECTOR_DIMENSIONS,
            vector_search_profile_name=VECTOR_PROFILE_NAME,
        ),
    ]
    semantic_search = SemanticSearch(
        configurations=[
            SemanticConfiguration(
                name=SEMANTIC_CONFIG_NAME,
                prioritized_fields=SemanticPrioritizedFields(
                    content_fields=[SemanticField(field_name="content")],
                    keywords_fields=[
                        SemanticField(field_name="breadcrumb"),
                        SemanticField(field_name="summary"),
                    ],
                ),
            )
        ]
    )
    return SearchIndex(
        name=CHUNKS_INDEX_NAME,
        fields=fields,
        vector_search=build_vector_search(),
        semantic_search=semantic_search,
    )


def build_document_index() -> SearchIndex:
    fields = [
        SimpleField(name="fuzet_szam", type=SearchFieldDataType.String, key=True, filterable=True, facetable=True),
        SearchableField(
            name="fuzet_cim",
            analyzer_name="hu.microsoft",
            synonym_map_names=[SYNONYM_MAP_NAME],
        ),
        SimpleField(name="kozzeteve", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="total_pages", type=SearchFieldDataType.Int32, filterable=True, sortable=True),
        SimpleField(name="chunk_count", type=SearchFieldDataType.Int32, filterable=True, sortable=True),
        SearchableField(
            name="content_summary",
            analyzer_name="hu.microsoft",
            synonym_map_names=[SYNONYM_MAP_NAME],
        ),
        SearchField(
            name="document_vector",
            type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
            searchable=True,
            vector_search_dimensions=VECTOR_DIMENSIONS,
            vector_search_profile_name=VECTOR_PROFILE_NAME,
        ),
    ]
    semantic_search = SemanticSearch(
        configurations=[
            SemanticConfiguration(
                name=SEMANTIC_CONFIG_NAME,
                prioritized_fields=SemanticPrioritizedFields(
                    title_field=SemanticField(field_name="fuzet_cim"),
                    content_fields=[SemanticField(field_name="content_summary")],
                ),
            )
        ]
    )
    return SearchIndex(
        name=DOCUMENTS_INDEX_NAME,
        fields=fields,
        vector_search=build_vector_search(),
        semantic_search=semantic_search,
    )


def ensure_indexes(index_client: SearchIndexClient) -> None:
    ensure_synonym_map(index_client)
    for index in (build_chunk_index(), build_document_index()):
        retryable("create index", lambda current=index: index_client.create_or_update_index(current))
        logger.info("Index ready: %s", index.name)


def extract_fuzet_szam(file_name: str) -> str:
    match = re.match(r"^(\d{1,3})", file_name)
    return match.group(1) if match else Path(file_name).stem


def extract_kozzeteve(file_name: str) -> str | None:
    for pattern in DATE_PATTERNS:
        match = pattern.search(file_name)
        if match:
            return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    return None


def fallback_title_from_filename(file_name: str) -> str:
    base_name = Path(file_name).stem
    without_prefix = re.sub(r"^\d{1,3}_", "", base_name)
    without_date = re.sub(r"[_-]?20\d{2}(?:[_-]?\d{2}){0,2}$", "", without_prefix)
    return normalize_text(without_date.replace("_", " ").replace("-", " ")) or base_name


def paragraph_page(paragraph: Any) -> int:
    regions = getattr(paragraph, "bounding_regions", None) or []
    return int(regions[0].page_number) if regions else 1


def paragraph_order(paragraph: Any, fallback: int) -> int:
    spans = getattr(paragraph, "spans", None) or []
    return int(spans[0].offset) if spans else fallback


def table_pages(table: Any) -> tuple[int, int]:
    regions = getattr(table, "bounding_regions", None) or []
    if not regions:
        return 1, 1
    pages = sorted(int(region.page_number) for region in regions)
    return pages[0], pages[-1]


def table_order(table: Any, fallback: int) -> int:
    spans = getattr(table, "spans", None) or []
    return int(spans[0].offset) if spans else fallback


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

    header = [cell or " " for cell in grid[0]]
    divider = ["---"] * col_count
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(divider) + " |",
    ]
    for row in grid[1:]:
        lines.append("| " + " | ".join(cell or " " for cell in row) + " |")
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


def extract_ordered_elements(result: Any) -> list[OrderedElement]:
    ordered: list[OrderedElement] = []

    for index, paragraph in enumerate(getattr(result, "paragraphs", None) or []):
        role = getattr(paragraph, "role", None)
        if role in IGNORED_PARAGRAPH_ROLES:
            continue
        text = normalize_text(getattr(paragraph, "content", ""))
        if not text:
            continue
        page = paragraph_page(paragraph)
        ordered.append(
            OrderedElement(
                kind="paragraph",
                text=text,
                page_from=page,
                page_to=page,
                order=paragraph_order(paragraph, index),
                role=role,
            )
        )

    fallback_order = max((item.order for item in ordered), default=0) + 1
    for index, table in enumerate(getattr(result, "tables", None) or []):
        markdown = table_to_markdown(table)
        if not markdown:
            continue
        page_from, page_to = table_pages(table)
        ordered.append(
            OrderedElement(
                kind="table",
                text=markdown,
                page_from=page_from,
                page_to=page_to,
                order=table_order(table, fallback_order + index),
                role="table",
            )
        )

    return sorted(ordered, key=lambda item: (item.page_from, item.order, 0 if item.kind == "paragraph" else 1))


def build_chunk_id(file_name: str, breadcrumb: str, page_from: int, page_to: int, content_type: str, content: str) -> str:
    seed = f"{file_name}|{breadcrumb}|{page_from}|{page_to}|{content_type}|{content}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))


def build_chunk_payload(
    *,
    file_name: str,
    fuzet_szam: str,
    fuzet_cim: str,
    kozzeteve: str | None,
    breadcrumb: str,
    content_type: str,
    page_from: int,
    page_to: int,
    content: str,
) -> dict[str, Any]:
    references = sorted(set(REFERENCE_PATTERN.findall(content)))
    return {
        "chunk_id": build_chunk_id(file_name, breadcrumb, page_from, page_to, content_type, content),
        "fuzet_szam": fuzet_szam,
        "fuzet_cim": fuzet_cim,
        "breadcrumb": breadcrumb,
        "page_from": page_from,
        "page_to": page_to,
        "kozzeteve": kozzeteve,
        "hivatkozott_fuzetek": references,
        "content_type": content_type,
        "content": content,
    }


def chunk_sentence_entries(
    entries: list[dict[str, Any]],
    *,
    file_name: str,
    fuzet_szam: str,
    fuzet_cim: str,
    kozzeteve: str | None,
    breadcrumb: str,
) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    buffer: list[dict[str, Any]] = []

    def build_text_chunk(items: list[dict[str, Any]]) -> dict[str, Any]:
        body = " ".join(item["text"] for item in items).strip()
        chunk_text = f"{breadcrumb}\n\n{body}" if breadcrumb else body
        pages = [item["page"] for item in items]
        return build_chunk_payload(
            file_name=file_name,
            fuzet_szam=fuzet_szam,
            fuzet_cim=fuzet_cim,
            kozzeteve=kozzeteve,
            breadcrumb=breadcrumb,
            content_type="text",
            page_from=min(pages),
            page_to=max(pages),
            content=chunk_text,
        )

    for entry in entries:
        candidate = buffer + [entry]
        candidate_text = f"{breadcrumb}\n\n" + " ".join(item["text"] for item in candidate)
        if buffer and token_count(candidate_text) > TOKEN_TARGET:
            chunks.append(build_text_chunk(buffer))
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
        chunks.append(build_text_chunk(buffer))
    return chunks


def build_chunks_for_document(file_name: str, result: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    ordered_elements = extract_ordered_elements(result)
    if not ordered_elements:
        raise PDFProcessingError("No paragraphs or tables were extracted from the PDF")

    fuzet_szam = extract_fuzet_szam(file_name)
    kozzeteve = extract_kozzeteve(file_name)
    headings = [item.text for item in ordered_elements if item.role in {"title", "sectionHeading"}]
    fuzet_cim = headings[0] if headings else fallback_title_from_filename(file_name)

    section_stack: list[tuple[int, str]] = [(1, fuzet_cim)]
    sentence_entries: list[dict[str, Any]] = []
    chunks: list[dict[str, Any]] = []

    def current_breadcrumb() -> str:
        return " / ".join(title for _, title in section_stack if title)

    def flush_sentences() -> None:
        nonlocal sentence_entries
        if not sentence_entries:
            return
        chunks.extend(
            chunk_sentence_entries(
                sentence_entries,
                file_name=file_name,
                fuzet_szam=fuzet_szam,
                fuzet_cim=fuzet_cim,
                kozzeteve=kozzeteve,
                breadcrumb=current_breadcrumb(),
            )
        )
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
            breadcrumb = current_breadcrumb()
            table_text = f"{breadcrumb}\n\n{element.text}" if breadcrumb else element.text
            chunks.append(
                build_chunk_payload(
                    file_name=file_name,
                    fuzet_szam=fuzet_szam,
                    fuzet_cim=fuzet_cim,
                    kozzeteve=kozzeteve,
                    breadcrumb=breadcrumb,
                    content_type="table",
                    page_from=element.page_from,
                    page_to=element.page_to,
                    content=table_text,
                )
            )
            continue

        paragraph_text = element.text
        if element.role == "footnote":
            paragraph_text = f"Footnote: {paragraph_text}"
        for sentence in sentence_split(paragraph_text):
            sentence_entries.append({"text": sentence, "page": element.page_from})

    flush_sentences()

    combined_content = "\n\n".join(chunk["content"] for chunk in chunks)
    document = {
        "fuzet_szam": fuzet_szam,
        "fuzet_cim": fuzet_cim,
        "kozzeteve": kozzeteve,
        "total_pages": len(getattr(result, "pages", None) or []),
        "chunk_count": len(chunks),
        "content_summary": normalize_text(combined_content[:500]),
        "_document_embedding_source": f"{fuzet_cim} {normalize_text(combined_content[:500])}".strip(),
    }
    return chunks, document


def analyze_pdf(doc_client: DocumentIntelligenceClient, pdf_path: Path) -> Any:
    pdf_bytes = pdf_path.read_bytes()
    return retryable(
        f"analyze {pdf_path.name}",
        lambda: doc_client.begin_analyze_document(
            "prebuilt-layout",
            AnalyzeDocumentRequest(bytes_source=pdf_bytes),
            output_content_format=DocumentContentFormat.MARKDOWN,
            locale="hu-HU",
        ).result(),
    )


def summarize_chunk(chat_client: ChatCompletionsClient, settings: Settings, chunk: dict[str, Any]) -> str:
    prompt = (
        "Készíts pontosan 1 rövid magyar mondatos összefoglalót az alábbi NAV részletről. "
        "Őrizd meg az adózási terminológiát, és ne találj ki részleteket.\n\n"
        f"Címsorok: {chunk['breadcrumb']}\n\n"
        f"Tartalom:\n{chunk['content'][:6000]}"
    )

    response = retryable(
        f"summarize {chunk['chunk_id']}",
        lambda: chat_client.complete(
            messages=[
                SystemMessage(content="Tömör, magyar nyelvű retrieval összefoglalókat készítesz NAV tudásbázis-részletekhez."),
                UserMessage(content=prompt),
            ],
            temperature=0.1,
            max_tokens=120,
        ),
    )
    return normalize_text(response.choices[0].message.content)


def embed_texts(embedding_client: EmbeddingsClient, settings: Settings, texts: list[str]) -> list[list[float]]:
    response = retryable(
        "generate embeddings",
        lambda: embedding_client.embed(input=texts),
    )
    return [list(item.embedding) for item in response.data]


def enrich_chunk_documents(
    chunks: list[dict[str, Any]],
    *,
    chat_client: ChatCompletionsClient,
    embedding_client: EmbeddingsClient,
    settings: Settings,
    skip_summary: bool,
) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    for chunk in tqdm(chunks, desc="Summaries", leave=False, disable=len(chunks) == 0):
        summary = ""
        if not skip_summary:
            summary = summarize_chunk(chat_client, settings, chunk)
        prepared.append(
            {
                **chunk,
                "summary": summary,
                "_embedding_input": "\n".join(
                    part for part in [chunk.get("breadcrumb", ""), summary, chunk.get("content", "")] if part
                ),
            }
        )

    for batch in batched(prepared, EMBED_BATCH_SIZE):
        embeddings = embed_texts(
            embedding_client,
            settings,
            [str(item["_embedding_input"]) for item in batch],
        )
        for item, embedding in zip(batch, embeddings, strict=True):
            item["content_vector"] = embedding
            item.pop("_embedding_input", None)
    return prepared


def enrich_document_record(
    document: dict[str, Any],
    *,
    embedding_client: EmbeddingsClient,
    settings: Settings,
) -> dict[str, Any]:
    embedding_source = str(document.pop("_document_embedding_source"))
    document["document_vector"] = embed_texts(embedding_client, settings, [embedding_source])[0]
    return document


def ensure_upload_succeeded(results: list[Any], batch_label: str) -> None:
    failed = [result for result in results if not getattr(result, "succeeded", False)]
    if failed:
        keys = ", ".join(str(getattr(item, "key", "unknown")) for item in failed[:5])
        raise RuntimeError(f"Upload failed for {batch_label}: {keys}")


def upload_chunks(chunk_client: SearchClient, chunks: list[dict[str, Any]]) -> None:
    for batch in batched(chunks, UPLOAD_BATCH_SIZE):
        results = retryable(
            "upload chunk batch",
            lambda current=batch: chunk_client.merge_or_upload_documents(documents=current),
        )
        ensure_upload_succeeded(list(results), "chunks")


def upload_document(document_client: SearchClient, document: dict[str, Any]) -> None:
    results = retryable(
        "upload document",
        lambda: document_client.merge_or_upload_documents(documents=[document]),
    )
    ensure_upload_succeeded(list(results), "documents")


def search_escaped(value: str) -> str:
    return value.replace("'", "''")


def booklet_exists(chunk_client: SearchClient, fuzet_szam: str) -> bool:
    filter_expression = f"fuzet_szam eq '{search_escaped(fuzet_szam)}'"
    results = retryable(
        f"check existing booklet {fuzet_szam}",
        lambda: chunk_client.search(search_text="*", filter=filter_expression, top=1, select=["chunk_id"]),
    )
    return next(iter(results), None) is not None


def collect_pdf_paths(data_dir: Path, max_pdfs: int | None) -> list[Path]:
    if not data_dir.exists():
        raise FileNotFoundError(f"Data directory not found: {data_dir}")
    pdf_paths = sorted(path for path in data_dir.iterdir() if path.is_file() and path.suffix.lower() == ".pdf")
    if max_pdfs is not None:
        return pdf_paths[:max_pdfs]
    return pdf_paths


def process_pdf(
    pdf_path: Path,
    *,
    clients: AzureClients,
    settings: Settings,
    skip_summary: bool,
) -> tuple[int, dict[str, Any]]:
    result = analyze_pdf(clients.doc_client, pdf_path)
    chunks, document = build_chunks_for_document(pdf_path.name, result)
    if not chunks:
        raise PDFProcessingError("No chunks were created")
    chunk_docs = enrich_chunk_documents(
        chunks,
        chat_client=clients.chat_client,
        embedding_client=clients.embedding_client,
        settings=settings,
        skip_summary=skip_summary,
    )
    document_doc = enrich_document_record(document, embedding_client=clients.embedding_client, settings=settings)
    upload_chunks(clients.chunk_client, chunk_docs)
    upload_document(clients.document_client, document_doc)
    return len(chunk_docs), document_doc


def main() -> int:
    args = parse_args()
    configure_logging()
    settings = Settings.from_env()
    data_dir = resolve_data_dir(args.data_dir)
    pdf_paths = collect_pdf_paths(data_dir, args.max_pdfs)
    if not pdf_paths:
        logger.warning("No PDF files found in %s", data_dir)
        return 0

    clients = build_clients(settings)
    processed = 0
    skipped = 0
    failed = 0
    total_chunks = 0

    logger.info("Using data directory: %s", data_dir)
    logger.info("Found %s PDF file(s)", len(pdf_paths))
    logger.info("Creating or updating Azure AI Search indexes")
    ensure_indexes(clients.index_client)

    try:
        for pdf_path in tqdm(pdf_paths, desc="PDFs"):
            fuzet_szam = extract_fuzet_szam(pdf_path.name)
            if args.skip_existing and booklet_exists(clients.chunk_client, fuzet_szam):
                logger.info("Skipping %s because chunks already exist for booklet %s", pdf_path.name, fuzet_szam)
                skipped += 1
                continue

            try:
                chunk_count, document = process_pdf(
                    pdf_path,
                    clients=clients,
                    settings=settings,
                    skip_summary=args.no_summary,
                )
                processed += 1
                total_chunks += chunk_count
                logger.info(
                    "Processed %s -> %s chunk(s), %s page(s)",
                    pdf_path.name,
                    chunk_count,
                    document["total_pages"],
                )
            except Exception as exc:
                failed += 1
                logger.exception("Failed to process %s: %s", pdf_path.name, exc)
                continue
    finally:
        close_clients(clients)

    logger.info(
        "Complete. processed=%s skipped=%s failed=%s uploaded_chunks=%s",
        processed,
        skipped,
        failed,
        total_chunks,
    )
    return 0 if processed or skipped else 1


if __name__ == "__main__":
    sys.exit(main())
