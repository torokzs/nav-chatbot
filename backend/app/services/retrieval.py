import logging
from collections.abc import Iterable

from azure.ai.inference.aio import EmbeddingsClient
from azure.core.exceptions import HttpResponseError
from azure.identity.aio import DefaultAzureCredential
from azure.search.documents.aio import SearchClient
from azure.search.documents.models import QueryType, VectorizedQuery, VectorQuery

from app.config import Settings, get_settings
from app.models.schemas import ChunkResult, DocumentResult

logger = logging.getLogger(__name__)

_DOCUMENT_VECTOR_FIELD = "document_vector"
_CHUNK_VECTOR_FIELD = "content_vector"
_DOCUMENT_TOP_K = 3
_CHUNK_TOP_K = 8
_GLOBAL_CHUNK_TOP_K = 5
_FINAL_CONTEXT_TOP_K = 10
_EXPANDED_SECTION_TOP_K = 24
_SEMANTIC_CONFIGURATION_NAME = "default"


class RetrievalService:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        credential: DefaultAzureCredential | None = None,
        documents_client: SearchClient | None = None,
        chunks_client: SearchClient | None = None,
        embedding_client: EmbeddingsClient | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._credential = credential or DefaultAzureCredential()
        self._documents_client = documents_client or SearchClient(
            endpoint=self._settings.azure_search_endpoint,
            index_name=self._settings.azure_search_index_documents,
            credential=self._credential,
        )
        self._chunks_client = chunks_client or SearchClient(
            endpoint=self._settings.azure_search_endpoint,
            index_name=self._settings.azure_search_index_chunks,
            credential=self._credential,
        )
        self._embedding_client = embedding_client or EmbeddingsClient(
            endpoint=f"{self._settings.azure_ai_foundry_endpoint.rstrip('/')}/openai/deployments/{self._settings.azure_ai_foundry_embedding_deployment}",
            credential=self._credential,
            credential_scopes=["https://cognitiveservices.azure.com/.default"],
        )

    async def close(self) -> None:
        await self._documents_client.close()
        await self._chunks_client.close()
        await self._embedding_client.close()
        await self._credential.close()

    async def retrieve(
        self,
        query: str,
        rewritten_queries: list[str],
    ) -> tuple[list[ChunkResult], list[DocumentResult]]:
        queries = self._build_query_set(query, rewritten_queries)
        document_results = await self._retrieve_documents(queries)
        booklet_ids = [document.fuzet_szam for document in document_results if document.fuzet_szam]

        chunk_results = await self._retrieve_chunks(queries, booklet_ids)
        global_chunks = await self._retrieve_chunks(queries, [], top_k=_GLOBAL_CHUNK_TOP_K)

        all_chunks = self._deduplicate_chunks([*chunk_results, *global_chunks])
        all_chunks.sort(key=lambda item: item.score, reverse=True)
        top_chunks = all_chunks[:_FINAL_CONTEXT_TOP_K]

        found_booklet_ids = {
            str(chunk.metadata.get("fuzet_szam", ""))
            for chunk in top_chunks
            if chunk.metadata.get("fuzet_szam")
        }
        existing_booklet_ids = {document.fuzet_szam for document in document_results}
        for booklet_id in found_booklet_ids - existing_booklet_ids:
            for chunk in top_chunks:
                if str(chunk.metadata.get("fuzet_szam", "")) == booklet_id:
                    document_results.append(
                        DocumentResult(
                            fuzet_szam=booklet_id,
                            fuzet_cim=str(chunk.metadata.get("fuzet_cim", "")),
                            score=chunk.score,
                        )
                    )
                    break

        document_results.sort(key=lambda item: item.score, reverse=True)
        return top_chunks, document_results

    def _build_query_set(self, query: str, rewritten_queries: list[str]) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for candidate in [query, *rewritten_queries]:
            normalized = " ".join(candidate.split())
            if normalized and normalized.casefold() not in seen:
                seen.add(normalized.casefold())
                ordered.append(normalized)
        return ordered or [query]

    async def _retrieve_documents(self, queries: list[str]) -> list[DocumentResult]:
        search_results = await self._documents_client.search(
            search_text=None,
            top=_DOCUMENT_TOP_K,
            vector_queries=await self._build_vector_queries(
                queries,
                field_name=_DOCUMENT_VECTOR_FIELD,
                k_nearest_neighbors=_DOCUMENT_TOP_K,
            ),
        )
        documents: dict[str, DocumentResult] = {}
        async for result in search_results:
            mapped = self._map_document(result)
            current = documents.get(mapped.fuzet_szam)
            if current is None or mapped.score > current.score:
                documents[mapped.fuzet_szam] = mapped
        ranked_documents = sorted(
            documents.values(),
            key=lambda item: item.score,
            reverse=True,
        )
        return ranked_documents[:_DOCUMENT_TOP_K]

    async def _retrieve_chunks(
        self,
        queries: list[str],
        booklet_ids: list[str],
        *,
        top_k: int = _CHUNK_TOP_K,
    ) -> list[ChunkResult]:
        filter_expression = self._build_booklet_filter(booklet_ids)
        search_text = " ".join(queries)
        vector_queries = await self._build_vector_queries(
            queries,
            field_name=_CHUNK_VECTOR_FIELD,
            k_nearest_neighbors=top_k,
        )
        try:
            search_results = await self._chunks_client.search(
                search_text=search_text,
                filter=filter_expression,
                top=top_k,
                vector_queries=vector_queries,
                query_type=QueryType.SEMANTIC,
                semantic_configuration_name=_SEMANTIC_CONFIGURATION_NAME,
            )
        except HttpResponseError:
            logger.warning("Semantic search unavailable, falling back to standard hybrid search")
            search_results = await self._chunks_client.search(
                search_text=search_text,
                filter=filter_expression,
                top=top_k,
                vector_queries=vector_queries,
            )

        chunks: list[ChunkResult] = []
        async for result in search_results:
            chunks.append(self._map_chunk(result))
        return chunks

    async def _expand_parent_sections(
        self,
        booklet_ids: list[str],
        chunks: list[ChunkResult],
    ) -> list[ChunkResult]:
        """Expand chunks to include sibling chunks from the same section.

        Falls back gracefully if the filter expression is not supported.
        """
        section_prefixes = {
            self._section_prefix(chunk.metadata.get("breadcrumb", ""))
            for chunk in chunks
            if chunk.metadata.get("breadcrumb")
        }
        section_prefixes.discard("")
        if not booklet_ids or not section_prefixes:
            return []

        filter_expression = self._build_section_filter(booklet_ids, section_prefixes)
        try:
            search_results = await self._chunks_client.search(
                search_text="*",
                filter=filter_expression,
                top=_EXPANDED_SECTION_TOP_K,
            )
            expanded: list[ChunkResult] = []
            async for result in search_results:
                expanded.append(self._map_chunk(result))
            return expanded
        except (HttpResponseError, Exception) as exc:
            logger.warning("Section expansion failed: %s", exc)
            return []

    async def _build_vector_queries(
        self,
        queries: list[str],
        *,
        field_name: str,
        k_nearest_neighbors: int,
    ) -> list[VectorQuery]:
        vector_queries: list[VectorQuery] = []
        for query in queries:
            vector_queries.append(
                VectorizedQuery(
                    vector=await self._embed_query(query),
                    fields=field_name,
                    k_nearest_neighbors=k_nearest_neighbors,
                    weight=1.0,
                )
            )
        return vector_queries

    async def _embed_query(self, query: str) -> list[float]:
        result = await self._embedding_client.embed(
            input=[query],
        )
        embedding = result.data[0].embedding
        if isinstance(embedding, list):
            return [float(item) for item in embedding]
        return []

    def _map_document(self, payload: dict[str, object]) -> DocumentResult:
        return DocumentResult(
            fuzet_szam=str(
                self._pick(payload, "fuzet_szam", "booklet_id", "document_id", default="")
            ),
            fuzet_cim=str(
                self._pick(payload, "fuzet_cim", "title", "document_title", default="")
            ),
            score=self._score_from_payload(payload),
        )

    def _map_chunk(self, payload: dict[str, object]) -> ChunkResult:
        page_from = self._to_int(
            self._pick(payload, "page_from", "page", "page_start", default=0)
        )
        page_to = self._to_int(
            self._pick(payload, "page_to", "page", "page_end", default=page_from)
        )
        metadata = {
            "fuzet_szam": str(
                self._pick(payload, "fuzet_szam", "booklet_id", "document_id", default="")
            ),
            "fuzet_cim": str(
                self._pick(payload, "fuzet_cim", "title", "document_title", default="")
            ),
            "page_from": page_from,
            "page_to": page_to,
            "breadcrumb": str(
                self._pick(payload, "breadcrumb", "section_path", "section", default="")
            ),
        }
        return ChunkResult(
            content=str(self._pick(payload, "content", "chunk", "text", "body", default="")),
            metadata=metadata,
            score=self._score_from_payload(payload),
        )

    def _score_from_payload(self, payload: dict[str, object]) -> float:
        raw_score = payload.get("@search.reranker_score") or payload.get("@search.score") or 0.0
        if isinstance(raw_score, (int, float, str)):
            try:
                return float(raw_score)
            except ValueError:
                return 0.0
        return 0.0

    def _pick(self, payload: dict[str, object], *keys: str, default: object) -> object:
        for key in keys:
            if key in payload and payload[key] is not None:
                return payload[key]
        return default

    def _build_booklet_filter(self, booklet_ids: list[str]) -> str | None:
        normalized = [
            self._escape_odata_value(booklet_id)
            for booklet_id in booklet_ids
            if booklet_id
        ]
        if not normalized:
            return None
        return f"search.in(fuzet_szam, '{','.join(normalized)}', ',')"

    def _build_section_filter(self, booklet_ids: list[str], section_prefixes: Iterable[str]) -> str:
        booklet_filter = self._build_booklet_filter(booklet_ids)
        # Use search.ismatch with wildcard for prefix matching (startswith not supported)
        prefix_filters = " or ".join(
            f"search.ismatch('{self._escape_odata_value(prefix)}*', 'breadcrumb')"
            for prefix in section_prefixes
        )
        if booklet_filter:
            return f"({booklet_filter}) and ({prefix_filters})"
        return prefix_filters

    def _section_prefix(self, breadcrumb: str) -> str:
        for separator in (" > ", " » ", "/"):
            if separator in breadcrumb:
                parts = [part.strip() for part in breadcrumb.split(separator) if part.strip()]
                if len(parts) > 1:
                    return separator.join(parts[:-1])
        return breadcrumb.strip()

    def _deduplicate_chunks(self, chunks: list[ChunkResult]) -> list[ChunkResult]:
        deduplicated: dict[tuple[str, str, int, int, str], ChunkResult] = {}
        for chunk in chunks:
            key = (
                str(chunk.metadata.get("fuzet_szam", "")),
                str(chunk.metadata.get("breadcrumb", "")),
                self._to_int(chunk.metadata.get("page_from", 0)),
                self._to_int(chunk.metadata.get("page_to", 0)),
                chunk.content,
            )
            current = deduplicated.get(key)
            if current is None or chunk.score > current.score:
                deduplicated[key] = chunk
        return list(deduplicated.values())

    def _escape_odata_value(self, value: str) -> str:
        return value.replace("'", "''")

    def _to_int(self, value: object) -> int:
        if isinstance(value, (int, float, str)):
            try:
                return int(value)
            except ValueError:
                return 0
        return 0
