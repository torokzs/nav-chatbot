import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from app.config import Settings


class MockAsyncSearchResults:
    def __init__(self, items: list[dict[str, Any]]) -> None:
        self._items = items
        self._index = 0

    def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        self._index = 0
        return self

    async def __anext__(self) -> dict[str, Any]:
        if self._index >= len(self._items):
            raise StopAsyncIteration
        item = self._items[self._index]
        self._index += 1
        return item


class MockSearchClient:
    def __init__(self, responses: list[list[dict[str, Any]]] | None = None) -> None:
        self._responses = responses or []
        self.calls: list[dict[str, Any]] = []

    def search(self, *args: Any, **kwargs: Any) -> MockAsyncSearchResults:
        self.calls.append({"args": args, "kwargs": kwargs})
        response = self._responses.pop(0) if self._responses else []
        return MockAsyncSearchResults(response)

    async def close(self) -> None:
        return None


class MockLLMClient:
    def __init__(self, tokens: list[str] | None = None) -> None:
        self.tokens = tokens or ["teszt"]
        self.calls: list[dict[str, Any]] = []

    async def complete(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        self.calls.append({"args": args, "kwargs": kwargs})

        async def _stream() -> AsyncIterator[Any]:
            for token in self.tokens:
                yield SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content=token))]
                )

        return _stream()

    async def close(self) -> None:
        return None


@pytest.fixture(scope="session")
def event_loop() -> Any:
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def mock_settings() -> Settings:
    return Settings(
        azure_search_endpoint="https://search.example.com",
        azure_search_index_chunks="chunks",
        azure_search_index_documents="documents",
        azure_ai_foundry_endpoint="https://foundry.example.com",
        azure_ai_foundry_chat_deployment="model-router",
        azure_ai_foundry_embedding_deployment="text-embedding-3-large",
        azure_key_vault_url="https://vault.example.com",
        applicationinsights_connection_string="InstrumentationKey=test",
        rate_limit_rpm=30,
        max_input_length=2000,
        frontend_origin="http://localhost:3000",
    )


@pytest.fixture
def mock_search_client() -> MockSearchClient:
    return MockSearchClient()


@pytest.fixture
def mock_llm_client() -> MockLLMClient:
    return MockLLMClient()
