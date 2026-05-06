import json
import logging
import os
import re
from collections.abc import AsyncGenerator
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sse_starlette.sse import EventSourceResponse

from app.middleware.input_guard import guard_chat_request
from app.middleware.rate_limit import enforce_rate_limit
from app.models.schemas import ChatEvent, ChatRequest
from app.services.citation import format_citations
from app.services.llm import LLMService
from app.services.query_rewrite import rewrite_query
from app.services.retrieval import RetrievalService

logger = logging.getLogger(__name__)
router = APIRouter(tags=["chat"])
DATA_DIR = Path(os.environ.get("PDF_DATA_DIR", "/data"))


def get_retrieval_service(request: Request) -> RetrievalService:
    return request.app.state.retrieval_service  # type: ignore[no-any-return]


def get_llm_service(request: Request) -> LLMService:
    return request.app.state.llm_service  # type: ignore[no-any-return]


def _to_sse_message(event: ChatEvent) -> dict[str, str]:
    return {
        "data": json.dumps(event.model_dump(mode="json"), ensure_ascii=False),
    }


@router.get("/api/documents/{fuzet_szam}/pdf")
async def get_document_pdf(fuzet_szam: str) -> FileResponse:
    """Serve a NAV booklet PDF by its number."""
    for pdf_file in DATA_DIR.glob("*.pdf"):
        if re.match(rf"^0*{re.escape(fuzet_szam)}_", pdf_file.name) or pdf_file.name.startswith(
            f"{fuzet_szam}_"
        ):
            return FileResponse(
                path=pdf_file,
                media_type="application/pdf",
                filename=pdf_file.name,
            )
    raise HTTPException(status_code=404, detail=f"PDF not found for booklet {fuzet_szam}")


@router.post("/api/chat", response_class=EventSourceResponse)
async def stream_chat(
    payload: ChatRequest = Depends(guard_chat_request),  # noqa: B008
    _: None = Depends(enforce_rate_limit),  # noqa: B008
    retrieval_service: RetrievalService = Depends(get_retrieval_service),  # noqa: B008
    llm_service: LLMService = Depends(get_llm_service),  # noqa: B008
) -> EventSourceResponse:
    async def event_stream() -> AsyncGenerator[dict[str, str], None]:
        try:
            rewritten_queries = rewrite_query(payload.message)
            context_chunks, source_documents = await retrieval_service.retrieve(
                payload.message,
                rewritten_queries,
            )

            async for token in llm_service.generate_response(
                payload.message,
                context_chunks,
                source_documents,
            ):
                yield _to_sse_message(ChatEvent(type="token", content=token))

            citations = format_citations(context_chunks)
            yield _to_sse_message(
                ChatEvent(
                    type="sources",
                    content=[citation.model_dump(mode="json") for citation in citations],
                )
            )
            yield _to_sse_message(ChatEvent(type="done", content=None))
        except Exception:
            logger.exception("Chat request failed")
            yield _to_sse_message(
                ChatEvent(
                    type="error",
                    content="Belső hiba történt a válasz feldolgozása közben.",
                )
            )

    return EventSourceResponse(event_stream(), ping=15)
