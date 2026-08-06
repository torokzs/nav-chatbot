import asyncio
import json
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient
from pytest import MonkeyPatch
from sse_starlette.sse import AppStatus

from app.config import Settings, get_settings
from app.main import create_app
from app.models.schemas import ChunkResult, DocumentResult
from app.routers import chat as chat_router


class FakeRetrievalService:
    def __init__(self, *, should_fail: bool = False) -> None:
        self.should_fail = should_fail

    async def retrieve(
        self,
        query: str,
        rewritten_queries: list[str],
        adoev: int,
    ) -> tuple[list[ChunkResult], list[DocumentResult]]:
        if self.should_fail:
            raise RuntimeError("search failure")
        return (
            [
                ChunkResult(
                    content="Az SZJA bevallás határideje május 20.",
                    metadata={
                        "adoev": adoev,
                        "fuzet_szam": "1",
                        "fuzet_cim": "SZJA információk",
                        "page_from": 10,
                        "page_to": 10,
                        "breadcrumb": "Bevallás > Határidők",
                    },
                    score=0.99,
                )
            ],
            [DocumentResult(adoev=adoev, fuzet_szam="1", fuzet_cim="SZJA információk", score=0.99)],
        )

    async def close(self) -> None:
        return None


class FakeLLMService:
    async def generate_response(
        self,
        query: str,
        context_chunks: list[ChunkResult],
        sources: list[DocumentResult],
    ) -> AsyncGenerator[str, None]:
        for token in ["A bevallás ", "határideje május 20."]:
            yield token

    async def close(self) -> None:
        return None


def _reset_sse_app_status() -> None:
    AppStatus.should_exit_event = cast(Any, asyncio.Event())


@pytest.mark.asyncio
async def test_chat_endpoint_streams_sse_events(mock_settings: Settings) -> None:
    _reset_sse_app_status()
    app = create_app()
    app.state.settings = mock_settings
    app.state.retrieval_service = FakeRetrievalService()
    app.state.llm_service = FakeLLMService()
    app.dependency_overrides[get_settings] = lambda: mock_settings

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client, client.stream(
        "POST",
        "/api/chat",
        json={"message": "Mikor kell beadni az SZJA bevallást?", "adoev": 2026},
    ) as response:
        assert response.status_code == 200
        events = [
            json.loads(line.removeprefix("data: "))
            async for line in response.aiter_lines()
            if line.startswith("data: ")
        ]

    assert [event["type"] for event in events] == ["token", "token", "sources", "done"]
    assert events[2]["content"][0]["fuzet_szam"] == "1"
    assert events[2]["content"][0]["adoev"] == 2026
    assert events[2]["content"][0]["url"] == "/api/documents/2026/1/pdf"


@pytest.mark.asyncio
async def test_chat_endpoint_includes_context_only_when_requested(
    mock_settings: Settings,
) -> None:
    _reset_sse_app_status()
    app = create_app()
    app.state.settings = mock_settings
    app.state.retrieval_service = FakeRetrievalService()
    app.state.llm_service = FakeLLMService()
    app.dependency_overrides[get_settings] = lambda: mock_settings

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client, client.stream(
        "POST",
        "/api/chat",
        json={
            "message": "Mikor kell beadni az SZJA bevallást?",
            "adoev": 2026,
            "include_evaluation_context": True,
        },
    ) as response:
        events = [
            json.loads(line.removeprefix("data: "))
            async for line in response.aiter_lines()
            if line.startswith("data: ")
        ]

    assert [event["type"] for event in events] == [
        "context",
        "token",
        "token",
        "sources",
        "done",
    ]
    assert events[0]["content"][0]["content"] == "Az SZJA bevallás határideje május 20."
    assert events[0]["content"][0]["score"] == 0.99


@pytest.mark.asyncio
async def test_chat_endpoint_retrieval_only_skips_generation(
    mock_settings: Settings,
) -> None:
    _reset_sse_app_status()
    app = create_app()
    app.state.settings = mock_settings
    app.state.retrieval_service = FakeRetrievalService()
    app.state.llm_service = FakeLLMService()
    app.dependency_overrides[get_settings] = lambda: mock_settings

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client, client.stream(
        "POST",
        "/api/chat",
        json={
            "message": "Mikor kell beadni az SZJA bevallást?",
            "adoev": 2026,
            "include_evaluation_context": True,
            "evaluation_retrieval_only": True,
        },
    ) as response:
        events = [
            json.loads(line.removeprefix("data: "))
            async for line in response.aiter_lines()
            if line.startswith("data: ")
        ]

    assert [event["type"] for event in events] == ["context", "sources", "done"]


@pytest.mark.asyncio
async def test_chat_endpoint_context_override_requires_run_token(
    mock_settings: Settings,
    monkeypatch: MonkeyPatch,
) -> None:
    _reset_sse_app_status()
    monkeypatch.setenv("BENCHMARK_CONTEXT_TOKEN", "run-secret")
    app = create_app()
    app.state.settings = mock_settings
    app.state.retrieval_service = FakeRetrievalService(should_fail=True)
    app.state.llm_service = FakeLLMService()
    app.dependency_overrides[get_settings] = lambda: mock_settings
    context = [
        {
            "content": "Az SZJA bevallás határideje május 20.",
            "metadata": {
                "adoev": 2026,
                "fuzet_szam": "1",
                "fuzet_cim": "SZJA információk",
                "page_from": 10,
                "page_to": 10,
                "breadcrumb": "Bevallás > Határidők",
            },
            "score": 0.99,
        }
    ]

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        forbidden = await client.post(
            "/api/chat",
            json={
                "message": "Mikor kell beadni az SZJA bevallást?",
                "adoev": 2026,
                "evaluation_context": context,
            },
        )
        async with client.stream(
            "POST",
            "/api/chat",
            headers={"x-benchmark-context-token": "run-secret"},
            json={
                "message": "Mikor kell beadni az SZJA bevallást?",
                "adoev": 2026,
                "evaluation_context": context,
            },
        ) as response:
            events = [
                json.loads(line.removeprefix("data: "))
                async for line in response.aiter_lines()
                if line.startswith("data: ")
            ]

    assert forbidden.status_code == 403
    assert [event["type"] for event in events] == [
        "token",
        "token",
        "sources",
        "done",
    ]


@pytest.mark.asyncio
async def test_chat_endpoint_streams_error_event(mock_settings: Settings) -> None:
    _reset_sse_app_status()
    app = create_app()
    app.state.settings = mock_settings
    app.state.retrieval_service = FakeRetrievalService(should_fail=True)
    app.state.llm_service = FakeLLMService()
    app.dependency_overrides[get_settings] = lambda: mock_settings

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client, client.stream(
        "POST",
        "/api/chat",
        json={"message": "Mikor kell beadni az SZJA bevallást?", "adoev": 2026},
    ) as response:
        events = [
            json.loads(line.removeprefix("data: "))
            async for line in response.aiter_lines()
            if line.startswith("data: ")
        ]

    assert response.status_code == 200
    assert len(events) == 1
    assert events[0]["type"] == "error"


@pytest.mark.asyncio
async def test_get_document_pdf_serves_matching_pdf(
    mock_settings: Settings, tmp_path: Path, monkeypatch: MonkeyPatch,
) -> None:
    _reset_sse_app_status()
    # Create a fake PDF in a temp directory
    fake_pdf = tmp_path / "01_TestBooklet_2026.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 fake content")
    monkeypatch.setattr(chat_router, "DATA_DIR", tmp_path)

    app = create_app()
    app.state.settings = mock_settings
    app.state.retrieval_service = FakeRetrievalService()
    app.state.llm_service = FakeLLMService()
    app.dependency_overrides[get_settings] = lambda: mock_settings

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/api/documents/2026/1/pdf")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"


@pytest.mark.asyncio
async def test_get_document_pdf_returns_404_for_missing_booklet(
    mock_settings: Settings,
    monkeypatch: MonkeyPatch,
) -> None:
    _reset_sse_app_status()
    app = create_app()
    app.state.settings = mock_settings
    app.state.retrieval_service = FakeRetrievalService()
    app.state.llm_service = FakeLLMService()
    app.dependency_overrides[get_settings] = lambda: mock_settings
    monkeypatch.setattr(chat_router, "DATA_DIR", chat_router.DATA_DIR / "does-not-exist")

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/api/documents/2026/999/pdf")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_chat_endpoint_rejects_missing_tax_year(mock_settings: Settings) -> None:
    app = create_app()
    app.state.settings = mock_settings
    app.state.retrieval_service = FakeRetrievalService()
    app.state.llm_service = FakeLLMService()
    app.dependency_overrides[get_settings] = lambda: mock_settings

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post("/api/chat", json={"message": "Mikor kell bevallani?"})

    assert response.status_code == 422
