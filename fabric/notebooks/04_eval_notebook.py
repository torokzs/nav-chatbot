# METADATA ----------
# title: 04 - Local RAG Evaluation
# description: Runs RAGAS-style evaluation metrics (recall@k, citation correctness) against the eval dataset

# COMMAND ----------

from __future__ import annotations

import json
import os
import re
import statistics
import time
from pathlib import Path
from typing import Any

from azure.ai.inference import ChatCompletionsClient, EmbeddingsClient
from azure.ai.inference.models import SystemMessage, UserMessage
from azure.core.exceptions import HttpResponseError
from azure.identity import DefaultAzureCredential
from azure.search.documents import SearchClient
from azure.search.documents.models import VectorizedQuery
from pyspark.sql import Row


AI_FOUNDRY_ENDPOINT = os.getenv("AI_FOUNDRY_ENDPOINT") or spark.conf.get("nav.ai_foundry.endpoint", None)
AI_FOUNDRY_CHAT_MODEL = os.getenv("AI_FOUNDRY_CHAT_MODEL") or spark.conf.get("nav.ai_foundry.chat_model", None)
AI_FOUNDRY_EMBED_MODEL = os.getenv("AI_FOUNDRY_EMBED_MODEL", "text-embedding-3-large")
AI_SEARCH_ENDPOINT = os.getenv("AI_SEARCH_ENDPOINT") or spark.conf.get("nav.ai_search.endpoint", None)
CHUNKS_INDEX_NAME = os.getenv("AI_SEARCH_CHUNKS_INDEX", "nav-chunks")
DOCUMENTS_INDEX_NAME = os.getenv("AI_SEARCH_DOCUMENTS_INDEX", "nav-documents")
EVAL_RESULTS_TABLE = os.getenv("EVAL_RESULTS_TABLE", "eval_results")
EVAL_DATASET_PATH = os.getenv("EVAL_DATASET_PATH", "")

if not AI_FOUNDRY_ENDPOINT:
    raise ValueError("Set AI_FOUNDRY_ENDPOINT or spark config nav.ai_foundry.endpoint before running the notebook.")
if not AI_FOUNDRY_CHAT_MODEL:
    raise ValueError("Set AI_FOUNDRY_CHAT_MODEL or spark config nav.ai_foundry.chat_model before running the notebook.")
if not AI_SEARCH_ENDPOINT:
    raise ValueError("Set AI_SEARCH_ENDPOINT or spark config nav.ai_search.endpoint before running the notebook.")

ABBREVIATIONS = {
    "szja": "szja személyi jövedelemadó",
    "áfa": "áfa általános forgalmi adó",
    "kata": "kata kisadózó vállalkozók tételes adója",
    "ekho": "ekho egyszerűsített közteherviselési hozzájárulás",
    "tao": "tao társasági adó",
    "szocho": "szocho szociális hozzájárulási adó",
    "tbj": "tbj társadalombiztosítási járulék",
    "kiva": "kiva kisvállalati adó",
}


# COMMAND ----------


def retryable(operation_name: str, func, retries: int = 5):
    for attempt in range(retries):
        try:
            return func()
        except HttpResponseError as exc:
            if exc.status_code in {408, 429, 500, 502, 503, 504} and attempt < retries - 1:
                delay_seconds = min(60, 2 ** attempt * 5)
                print(f"{operation_name} failed with {exc.status_code}. Retrying in {delay_seconds} seconds.")
                time.sleep(delay_seconds)
                continue
            raise



def build_clients() -> tuple[ChatCompletionsClient, EmbeddingsClient, SearchClient, SearchClient]:
    credential = DefaultAzureCredential()
    return (
        ChatCompletionsClient(endpoint=AI_FOUNDRY_ENDPOINT, credential=credential),
        EmbeddingsClient(endpoint=AI_FOUNDRY_ENDPOINT, credential=credential),
        SearchClient(endpoint=AI_SEARCH_ENDPOINT, index_name=DOCUMENTS_INDEX_NAME, credential=credential),
        SearchClient(endpoint=AI_SEARCH_ENDPOINT, index_name=CHUNKS_INDEX_NAME, credential=credential),
    )



def resolve_eval_dataset_path() -> Path:
    candidates = [
        EVAL_DATASET_PATH,
        r"C:\Users\jator\source\repos\navchatbotprod\evals\nav_eval.jsonl",
        r"C:\Users\jator\source\repos\navchatbotprod\evals\dataset.jsonl",
        "/lakehouse/default/Files/evals/nav_eval.jsonl",
        "/lakehouse/default/Files/evals/dataset.jsonl",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        if path.exists():
            return path
    raise FileNotFoundError("No evaluation dataset was found. Set EVAL_DATASET_PATH to a JSONL file.")



def load_eval_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            rows.append(json.loads(text))
    return rows


# COMMAND ----------


def rewrite_query(question: str) -> str:
    normalized = re.sub(r"\s+", " ", question.strip())
    expanded = normalized
    for short_form, long_form in ABBREVIATIONS.items():
        expanded = re.sub(rf"\b{re.escape(short_form)}\b", long_form, expanded, flags=re.IGNORECASE)
    return expanded



def embed_query(embedding_client: EmbeddingsClient, text: str) -> list[float]:
    def run() -> Any:
        return embedding_client.embed(input=[text], model=AI_FOUNDRY_EMBED_MODEL)

    response = retryable("query embedding", run)
    return response.data[0].embedding



def retrieve_documents(document_search_client: SearchClient, embedding_client: EmbeddingsClient, query: str, top: int = 3) -> list[dict[str, Any]]:
    vector_query = VectorizedQuery(vector=embed_query(embedding_client, query), k_nearest_neighbors=top, fields="document_vector")
    results = document_search_client.search(
        search_text=query,
        vector_queries=[vector_query],
        query_type="semantic",
        semantic_configuration_name="nav-documents-semantic",
        top=top,
    )
    return [dict(item) for item in results]



def build_filter(document_hits: list[dict[str, Any]]) -> str | None:
    if not document_hits:
        return None
    allowed = ",".join(hit["fuzet_szam"] for hit in document_hits)
    return f"search.in(fuzet_szam, '{allowed}', ',')"



def retrieve_chunks(
    chunk_search_client: SearchClient,
    embedding_client: EmbeddingsClient,
    query: str,
    document_hits: list[dict[str, Any]],
    top: int = 10,
) -> list[dict[str, Any]]:
    vector_query = VectorizedQuery(vector=embed_query(embedding_client, query), k_nearest_neighbors=top, fields="content_vector")
    results = chunk_search_client.search(
        search_text=query,
        vector_queries=[vector_query],
        filter=build_filter(document_hits),
        query_type="semantic",
        semantic_configuration_name="nav-chunks-semantic",
        scoring_profile="bm25-profile",
        top=top,
    )
    return [dict(item) for item in results]


# COMMAND ----------


def generate_answer(chat_client: ChatCompletionsClient, question: str, chunks: list[dict[str, Any]]) -> str:
    context_lines = []
    for item in chunks[:5]:
        context_lines.append(
            f"[{item['fuzet_szam']}, {item['page_from']}-{item['page_to']}. oldal] {item['content'][:1600]}"
        )
    prompt = (
        "Answer the user's question in Hungarian using only the provided context. "
        "Always include citations in the form [fuzet_szam, page-page. oldal].\n\n"
        f"Question: {question}\n\n"
        "Context:\n"
        + "\n\n".join(context_lines)
    )

    def run() -> Any:
        return chat_client.complete(
            model=AI_FOUNDRY_CHAT_MODEL,
            temperature=0.1,
            max_tokens=600,
            messages=[
                SystemMessage("You answer Hungarian tax questions with precise citations."),
                UserMessage(prompt),
            ],
        )

    response = retryable("answer generation", run)
    return response.choices[0].message.content.strip()



def judge_relevance(chat_client: ChatCompletionsClient, question: str, expected_answer: str, actual_answer: str) -> int:
    prompt = (
        "Score the generated answer from 1 to 5 for relevance to the expected answer. "
        "Return only the integer.\n\n"
        f"Question: {question}\n\n"
        f"Expected answer: {expected_answer}\n\n"
        f"Generated answer: {actual_answer}"
    )

    def run() -> Any:
        return chat_client.complete(
            model=AI_FOUNDRY_CHAT_MODEL,
            temperature=0,
            max_tokens=10,
            messages=[
                SystemMessage("You are a strict evaluation judge."),
                UserMessage(prompt),
            ],
        )

    response = retryable("answer judging", run)
    text = response.choices[0].message.content.strip()
    match = re.search(r"[1-5]", text)
    return int(match.group(0)) if match else 1



def citation_is_correct(answer: str, expected_fuzet: str, expected_page: int) -> int:
    page_variants = {
        str(expected_page),
        f"{expected_page}-{expected_page}",
        f"{expected_page}. oldal",
    }
    if expected_fuzet not in answer:
        return 0
    return int(any(variant in answer for variant in page_variants))



def chunk_hit_found(chunks: list[dict[str, Any]], expected_fuzet: str, expected_page: int, top_k: int) -> int:
    for item in chunks[:top_k]:
        if item.get("fuzet_szam") == expected_fuzet and int(item.get("page_from", 0)) <= expected_page <= int(item.get("page_to", 0)):
            return 1
    return 0


# COMMAND ----------

chat_client, embedding_client, document_search_client, chunk_search_client = build_clients()
dataset_path = resolve_eval_dataset_path()
eval_rows = load_eval_rows(dataset_path)
print(f"Loaded {len(eval_rows)} evaluation rows from {dataset_path}")

results: list[dict[str, Any]] = []
for index, row in enumerate(eval_rows, start=1):
    question = row["question"]
    expected_answer = row.get("expected_answer", "")
    expected_fuzet = str(row["expected_fuzet"])
    expected_page = int(row["expected_page"])

    print(f"[{index}/{len(eval_rows)}] Evaluating: {question}")
    rewritten_query = rewrite_query(question)
    document_hits = retrieve_documents(document_search_client, embedding_client, rewritten_query, top=3)
    chunk_hits = retrieve_chunks(chunk_search_client, embedding_client, rewritten_query, document_hits, top=10)
    answer = generate_answer(chat_client, question, chunk_hits)
    relevance = judge_relevance(chat_client, question, expected_answer, answer)

    result_row = {
        "question": question,
        "rewritten_query": rewritten_query,
        "expected_answer": expected_answer,
        "expected_fuzet": expected_fuzet,
        "expected_page": expected_page,
        "document_recall_at_3": int(any(hit.get("fuzet_szam") == expected_fuzet for hit in document_hits[:3])),
        "chunk_recall_at_5": chunk_hit_found(chunk_hits, expected_fuzet, expected_page, 5),
        "chunk_recall_at_10": chunk_hit_found(chunk_hits, expected_fuzet, expected_page, 10),
        "citation_correctness": citation_is_correct(answer, expected_fuzet, expected_page),
        "answer_relevance": relevance,
        "generated_answer": answer,
        "retrieved_documents": json.dumps(document_hits, ensure_ascii=False),
        "retrieved_chunks": json.dumps(chunk_hits[:10], ensure_ascii=False),
    }
    results.append(result_row)

if not results:
    raise RuntimeError("No evaluation rows were processed.")

results_df = spark.createDataFrame([Row(**item) for item in results])
(
    results_df.write.format("delta")
    .mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(EVAL_RESULTS_TABLE)
)

summary = {
    "document_recall@3": statistics.mean(item["document_recall_at_3"] for item in results),
    "chunk_recall@5": statistics.mean(item["chunk_recall_at_5"] for item in results),
    "chunk_recall@10": statistics.mean(item["chunk_recall_at_10"] for item in results),
    "citation_correctness": statistics.mean(item["citation_correctness"] for item in results),
    "answer_relevance": statistics.mean(item["answer_relevance"] for item in results),
}

print("Evaluation summary:")
for metric_name, value in summary.items():
    print(f" - {metric_name}: {value:.3f}")

display(results_df)
