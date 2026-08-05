import json
import os
import sys
from collections.abc import Generator

import httpx
import pytest

BACKEND_URL = os.environ.get("BACKEND_URL", "").rstrip("/")
REQUEST_TIMEOUT = httpx.Timeout(30.0, connect=10.0, read=10.0)
STREAM_TIMEOUT_SECONDS = 30


@pytest.fixture(scope="session")
def backend_url() -> str:
    if not BACKEND_URL:
        pytest.fail("BACKEND_URL environment variable is required.")
    return BACKEND_URL


def _sse_events(response: httpx.Response) -> Generator[dict[str, object], None, None]:
    for line in response.iter_lines():
        if not line:
            continue
        if isinstance(line, bytes):
            line = line.decode("utf-8")
        if line.startswith("data: "):
            yield json.loads(line.removeprefix("data: "))


def test_health_endpoint(backend_url: str) -> None:
    with httpx.Client(base_url=backend_url, timeout=REQUEST_TIMEOUT) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_chat_endpoint_streams_tokens_and_sources(backend_url: str) -> None:
    token_events: list[dict[str, object]] = []
    sources_event: dict[str, object] | None = None

    try:
        with httpx.Client(base_url=backend_url, timeout=httpx.Timeout(STREAM_TIMEOUT_SECONDS, connect=10.0)) as client:
            with client.stream(
                "POST",
                "/api/chat",
                json={"message": "Mikor kell beadni az SZJA bevallást?", "adoev": 2026},
                headers={"Accept": "text/event-stream"},
            ) as response:
                assert response.status_code == 200
                assert response.headers.get("content-type", "").startswith("text/event-stream")

                for event in _sse_events(response):
                    event_type = event.get("type")
                    if event_type == "token":
                        token_events.append(event)
                    elif event_type == "sources":
                        sources_event = event
                    elif event_type == "error":
                        pytest.fail(f"Chat endpoint returned error event: {event}")
                    elif event_type == "done":
                        break
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        pytest.fail(f"SSE smoke test failed: {exc}")

    assert token_events, "Expected at least one token event from the SSE stream."
    assert sources_event is not None, "Expected a sources event in the SSE stream."
    citations = sources_event.get("content")
    assert isinstance(citations, list) and citations, "Expected at least one citation in the sources event."
    assert all(item.get("adoev") == 2026 for item in citations)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__]))
