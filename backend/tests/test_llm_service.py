from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, cast

from openai import AsyncAzureOpenAI

from app.config import Settings
from app.models.schemas import ChunkResult, DocumentResult
from app.services.llm import LLMService


class FakeCompletions:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> AsyncIterator[Any]:
        self.calls.append(kwargs)

        async def updates() -> AsyncIterator[Any]:
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(delta=SimpleNamespace(content="Első")),
                ]
            )
            yield SimpleNamespace(choices=[])
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(delta=SimpleNamespace(content=" válasz")),
                ]
            )

        return updates()


class FakeOpenAIClient:
    def __init__(self) -> None:
        self.completions = FakeCompletions()
        self.chat = SimpleNamespace(completions=self.completions)
        self.closed = False

    async def close(self) -> None:
        self.closed = True


async def test_generate_response_ignores_empty_router_stream_choices(
    mock_settings: Settings,
) -> None:
    fake_client = FakeOpenAIClient()
    service = LLMService(
        settings=mock_settings,
        client=cast(AsyncAzureOpenAI, fake_client),
    )
    chunks = [
        ChunkResult(
            content="A 2026-os szabály szövege.",
            metadata={
                "adoev": 2026,
                "fuzet_szam": "01",
                "fuzet_cim": "SZJA",
                "page_from": 10,
                "page_to": 10,
            },
            score=1.0,
        )
    ]
    sources = [
        DocumentResult(
            adoev=2026,
            fuzet_szam="01",
            fuzet_cim="SZJA",
            score=1.0,
        )
    ]

    tokens = [
        token
        async for token in service.generate_response(
            "Mi a szabály?",
            chunks,
            sources,
        )
    ]
    await service.close()

    assert tokens == ["Első", " válasz"]
    assert fake_client.completions.calls[0]["model"] == "model-router"
    assert fake_client.completions.calls[0]["stream"] is True
    assert fake_client.closed is True
