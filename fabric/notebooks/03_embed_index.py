# METADATA ----------
# title: 03 - Embed & Index to Azure AI Search
# description: Generates embeddings with text-embedding-3-large and syncs chunks + documents to Azure AI Search indexes
# Authentication: Uses mssparkutils.credentials.getToken() for embeddings,
#                 AI Search API key for index operations (Fabric can't get search.azure.com tokens)

# COMMAND ----------

from __future__ import annotations

import json
import os
import time
from typing import Any

import requests

try:
    from notebookutils import mssparkutils  # type: ignore
except ImportError:
    import mssparkutils  # type: ignore


EMBEDDING_ENDPOINT = os.getenv("EMBEDDING_ENDPOINT", "https://cog-whnqec-6otgod.cognitiveservices.azure.com")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-large")
SEARCH_ENDPOINT = os.getenv("AI_SEARCH_ENDPOINT", "https://srch-whnqec-6otgod.search.windows.net")
SEARCH_KEY = os.getenv("AI_SEARCH_KEY", "")  # Set via notebook parameters or env
CHUNKS_TABLE = os.getenv("CHUNKS_TABLE", "nav_chunks")
DOCUMENTS_TABLE = os.getenv("DOCUMENTS_TABLE", "nav_documents")
CHUNKS_INDEX_NAME = os.getenv("AI_SEARCH_CHUNKS_INDEX", "nav-chunks")
DOCUMENTS_INDEX_NAME = os.getenv("AI_SEARCH_DOCUMENTS_INDEX", "nav-documents")
EMBED_BATCH_SIZE = int(os.getenv("EMBED_BATCH_SIZE", "8"))
VECTOR_DIMENSIONS = 3072
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

if not SEARCH_KEY:
    raise ValueError("Set AI_SEARCH_KEY before running (mssparkutils.credentials.getToken for search.azure.com is unsupported in Fabric).")

# COMMAND ----------


def get_embedding_token() -> str:
    """Get Cognitive Services token via mssparkutils."""
    return mssparkutils.credentials.getToken("https://cognitiveservices.azure.com")


def embed_texts(texts: list[str], max_retries: int = 8) -> list[list[float]]:
    """Embed texts using Azure OpenAI embeddings REST API with retry on 429."""
    url = f"{EMBEDDING_ENDPOINT}/openai/deployments/{EMBEDDING_MODEL}/embeddings?api-version=2024-06-01"
    token = get_embedding_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"input": texts}

    for attempt in range(max_retries):
        resp = requests.post(url, headers=headers, json=payload, timeout=120)
        if resp.status_code == 200:
            data = resp.json()
            return [item["embedding"] for item in data["data"]]
        elif resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", str(2 ** attempt * 5)))
            print(f"  Rate limited. Waiting {retry_after}s (attempt {attempt+1}/{max_retries})")
            time.sleep(retry_after)
            token = get_embedding_token()  # refresh token
            headers["Authorization"] = f"Bearer {token}"
        else:
            resp.raise_for_status()
    raise RuntimeError(f"Failed to embed after {max_retries} retries")


def upload_to_search(index_name: str, documents: list[dict], batch_size: int = 100) -> int:
    """Upload documents to Azure AI Search using REST API."""
    url = f"{SEARCH_ENDPOINT}/indexes/{index_name}/docs/index?api-version=2023-11-01"
    headers = {"api-key": SEARCH_KEY, "Content-Type": "application/json"}
    total_uploaded = 0

    for i in range(0, len(documents), batch_size):
        batch = documents[i:i + batch_size]
        actions = [{"@search.action": "mergeOrUpload", **doc} for doc in batch]
        resp = requests.post(url, headers=headers, json={"value": actions}, timeout=120)
        if resp.status_code not in (200, 207):
            print(f"  Upload error {resp.status_code}: {resp.text[:500]}")
            resp.raise_for_status()
        total_uploaded += len(batch)
        print(f"  Uploaded batch {i // batch_size + 1} ({total_uploaded}/{len(documents)})")

    return total_uploaded


# COMMAND ----------
# Load data from Delta tables

chunk_rows = [row.asDict(recursive=True) for row in spark.table(CHUNKS_TABLE).collect()]
document_rows = [row.asDict(recursive=True) for row in spark.table(DOCUMENTS_TABLE).collect()]
print(f"Loaded {len(chunk_rows)} chunk rows and {len(document_rows)} document rows from Delta tables.")


# COMMAND ----------
# Embed chunks

print("Embedding chunks...")
chunk_documents: list[dict[str, Any]] = []

for i in range(0, len(chunk_rows), EMBED_BATCH_SIZE):
    batch = chunk_rows[i:i + EMBED_BATCH_SIZE]
    texts = []
    for row in batch:
        embed_input = "\n".join([
            row.get("breadcrumb") or "",
            row.get("summary") or "",
            row.get("content") or "",
        ]).strip()
        texts.append(embed_input)

    embeddings = embed_texts(texts)

    for row, emb in zip(batch, embeddings):
        refs = row.get("hivatkozott_fuzetek")
        if isinstance(refs, str):
            refs = json.loads(refs) if refs else []
        chunk_documents.append({
            "chunk_id": row["chunk_id"],
            "fuzet_szam": str(row["fuzet_szam"]),
            "fuzet_cim": row.get("fuzet_cim", ""),
            "breadcrumb": row.get("breadcrumb", ""),
            "page_from": int(row.get("page_from", 1)),
            "page_to": int(row.get("page_to", 1)),
            "kozzeteve": row.get("kozzeteve", ""),
            "hivatkozott_fuzetek": refs or [],
            "content_type": row.get("content_type", "text"),
            "content": row.get("content", ""),
            "summary": row.get("summary", ""),
            "content_vector": emb,
        })

    print(f"  Embedded {min(i + EMBED_BATCH_SIZE, len(chunk_rows))}/{len(chunk_rows)} chunks")

print(f"Prepared {len(chunk_documents)} chunk documents for indexing.")


# COMMAND ----------
# Embed documents

print("Embedding documents...")
doc_documents: list[dict[str, Any]] = []

for i in range(0, len(document_rows), EMBED_BATCH_SIZE):
    batch = document_rows[i:i + EMBED_BATCH_SIZE]
    texts = [f"{row['fuzet_cim']} {row.get('content_summary', '')}".strip() for row in batch]

    embeddings = embed_texts(texts)

    for row, emb in zip(batch, embeddings):
        doc_documents.append({
            "fuzet_szam": str(row["fuzet_szam"]),
            "fuzet_cim": row.get("fuzet_cim", ""),
            "kozzeteve": row.get("kozzeteve", ""),
            "total_pages": int(row.get("total_pages", 0)),
            "chunk_count": int(row.get("chunk_count", 0)),
            "content_summary": row.get("content_summary", ""),
            "document_vector": emb,
        })

    print(f"  Embedded {min(i + EMBED_BATCH_SIZE, len(document_rows))}/{len(document_rows)} documents")

print(f"Prepared {len(doc_documents)} document embeddings.")


# COMMAND ----------
# Upload to Azure AI Search

print("Uploading chunks to AI Search...")
upload_to_search(CHUNKS_INDEX_NAME, chunk_documents)

print("Uploading documents to AI Search...")
upload_to_search(DOCUMENTS_INDEX_NAME, doc_documents)

print("Embedding and indexing complete.")

